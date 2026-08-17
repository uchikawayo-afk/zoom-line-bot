import base64
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta

import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def sanitize_env(value: str) -> str:
    """環境変数からASCII以外の文字と前後の空白を除去"""
    return value.strip().encode("ascii", errors="ignore").decode("ascii")


LINE_CHANNEL_SECRET = sanitize_env(os.getenv("LINE_CHANNEL_SECRET", ""))
LINE_CHANNEL_ACCESS_TOKEN = sanitize_env(os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))
# 日報「再送」用：ad-dashboardに保存された最新の日報本文を取り出すエンドポイント
DAILY_REPORT_LATEST_URL = sanitize_env(os.getenv(
    "DAILY_REPORT_LATEST_URL",
    "https://ad-dashboard-27531714336.asia-northeast1.run.app/api/daily-report/latest",
))
DAILY_REPORT_SECRET = sanitize_env(os.getenv("DAILY_REPORT_SECRET", ""))
RESEND_KEYWORDS = ("再送", "日報", "日報再送", "にっぽう")
ZOOM_ACCOUNT_ID = sanitize_env(os.getenv("ZOOM_ACCOUNT_ID", ""))
ZOOM_CLIENT_ID = sanitize_env(os.getenv("ZOOM_CLIENT_ID", ""))
ZOOM_CLIENT_SECRET = sanitize_env(os.getenv("ZOOM_CLIENT_SECRET", ""))

# マルチユーザーZoom認証情報 (JSONパース失敗時は空辞書で続行)
_raw_creds = os.getenv("USER_ZOOM_CREDENTIALS", "")
try:
    USER_ZOOM_CREDENTIALS: dict = json.loads(_raw_creds) if _raw_creds else {}
    if USER_ZOOM_CREDENTIALS:
        logger.info(f"Loaded Zoom credentials for {len(USER_ZOOM_CREDENTIALS)} user(s)")
except (json.JSONDecodeError, TypeError) as e:
    logger.error(f"USER_ZOOM_CREDENTIALS parse error: {e}")
    USER_ZOOM_CREDENTIALS = {}

JST = pytz.timezone("Asia/Tokyo")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
APP_VERSION = "2026-08-17-group-id"


def parse_datetime(text: str) -> datetime | None:
    """
    日本語テキストから日時を解析する。
    対応パターン例:
      - 「4/30 18時から」
      - 「4/30 18:30」
      - 「明日 15時から」
      - 「今日 20時30分から」
      - 「30日 18時」
    """
    now = datetime.now(JST)

    # パターン1: 月/日 時 (例: 4/30 18時, 4/30 18時30分, 4/30 18:30)
    m = re.search(
        r"(\d{1,2})/(\d{1,2})\s*[^\d]*?(\d{1,2})(?:時|:)(?:(\d{2})分?)?",
        text,
    )
    if m:
        month, day, hour = int(m.group(1)), int(m.group(2)), int(m.group(3))
        minute = int(m.group(4)) if m.group(4) else 0
        year = now.year
        try:
            dt = JST.localize(datetime(year, month, day, hour, minute))
            if dt < now:
                dt = JST.localize(datetime(year + 1, month, day, hour, minute))
            return dt
        except ValueError:
            return None

    # パターン2: 明日 時 (例: 明日 18時, 明日18時30分)
    m = re.search(r"明日\s*(\d{1,2})時(?:(\d{2})分)?", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        tomorrow = now + timedelta(days=1)
        return JST.localize(datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute))

    # パターン3: 今日 時 (例: 今日 20時, 今日20時30分)
    m = re.search(r"今日\s*(\d{1,2})時(?:(\d{2})分)?", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        return JST.localize(datetime(now.year, now.month, now.day, hour, minute))

    # パターン4: 日 時 (例: 30日 18時, 30日18時30分)
    m = re.search(r"(\d{1,2})日\s*(\d{1,2})時(?:(\d{2})分)?", text)
    if m:
        day, hour = int(m.group(1)), int(m.group(2))
        minute = int(m.group(3)) if m.group(3) else 0
        try:
            dt = JST.localize(datetime(now.year, now.month, day, hour, minute))
            if dt < now:
                next_month = now.month % 12 + 1
                next_year = now.year + (1 if now.month == 12 else 0)
                dt = JST.localize(datetime(next_year, next_month, day, hour, minute))
            return dt
        except ValueError:
            return None

    return None


def get_zoom_credentials(user_id: str) -> tuple[str, str, str] | None:
    """ユーザー別のZoom認証情報を取得。未登録ならNone"""
    creds = USER_ZOOM_CREDENTIALS.get(user_id)
    if creds:
        return creds["account_id"], creds["client_id"], creds["client_secret"]
    return None


def get_zoom_access_token(user_id: str = "") -> str:
    creds = get_zoom_credentials(user_id) if user_id else None
    if creds:
        account_id, client_id, client_secret = creds
    else:
        account_id, client_id, client_secret = ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET
    url = (
        f"https://zoom.us/oauth/token"
        f"?grant_type=account_credentials&account_id={account_id}"
    )
    resp = requests.post(url, auth=(client_id, client_secret), timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_zoom_meeting(start_time: datetime, user_id: str = "", duration: int = 60) -> dict:
    token = get_zoom_access_token(user_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "topic": "LINEから作成したミーティング",
        "type": 2,  # スケジュールミーティング
        "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "duration": duration,
        "timezone": "Asia/Tokyo",
        "settings": {
            "host_video": True,
            "participant_video": True,
            "join_before_host": True,
            "waiting_room": False,
        },
    }
    resp = requests.post(
        "https://api.zoom.us/v2/users/me/meetings",
        json=payload,
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def validate_signature(body: str, signature: str) -> bool:
    """LINE Messaging API の X-Line-Signature を hmac-sha256 で検証"""
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_text(reply_token: str, text: str) -> None:
    """requestsでLINE Messaging APIに直接返信。本文は明示的にUTF-8で送る"""
    logger.info(f"Reply token: {repr(reply_token)}")
    logger.info(f"Message text: {repr(text)}")
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    resp = requests.post(
        LINE_REPLY_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=10,
    )
    if resp.status_code != 200:
        logger.error(f"LINE reply failed: {resp.status_code} {resp.text}")


@app.route("/healthz", methods=["GET"])
def healthz():
    """デプロイ確認用。GROUP_ID_CMD対応版が乗っていれば version が返る。"""
    return {"ok": True, "version": APP_VERSION}


@app.route("/webhook", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    if not validate_signature(body, signature):
        abort(400)
    try:
        events = json.loads(body).get("events", [])
    except json.JSONDecodeError:
        abort(400)
    for event in events:
        etype = event.get("type")
        if etype == "join":
            handle_join(event)
        elif etype == "message" and event.get("message", {}).get("type") == "text":
            handle_message(event)
    return "OK"


def source_ids(event) -> tuple[str, str, str]:
    """(source_type, group_or_room_id, user_id) を返す。camelCase/snake_case両対応。"""
    src = event.get("source", {})
    stype = src.get("type", "")
    gid = (
        src.get("groupId") or src.get("group_id")
        or src.get("roomId") or src.get("room_id") or ""
    )
    uid = src.get("userId") or src.get("user_id") or ""
    return stype, gid, uid


def handle_join(event):
    """グループ/複数人トークに招待されたとき、通知先IDをその場で返す。"""
    stype, gid, _ = source_ids(event)
    logger.info(f"join event: type={stype} id={gid}")
    reply_text(
        event["replyToken"],
        "通知用botを追加いただきありがとうございます。\n"
        f"このトークの通知先ID（{stype}Id）:\n{gid}\n\n"
        "※このIDを通知システム側に設定すると、ここに通知が届くようになります。\n"
        "※このトーク内では /id 以外のメッセージには反応しません。",
    )


def resend_daily_report() -> str:
    """ad-dashboard から最新の日報本文を取得して返す。取得失敗時は理由を返す。
    Macが寝ていても、直前に生成・送信済みの日報ならこのクラウド保存から再送できる。"""
    if not DAILY_REPORT_SECRET:
        return "日報の再送設定が未完了です（DAILY_REPORT_SECRET未設定）。管理者に連絡してください。"
    try:
        resp = requests.get(
            DAILY_REPORT_LATEST_URL,
            headers={"Authorization": f"Bearer {DAILY_REPORT_SECRET}"},
            timeout=15,
        )
    except Exception as e:
        return f"日報の取得に失敗しました（通信エラー）。\n{e}"
    if resp.status_code == 404:
        return "まだ日報が作成されていません（就寝中で未生成の可能性）。Macを開けると生成されます。"
    if resp.status_code != 200:
        return f"日報の取得に失敗しました（HTTP {resp.status_code}）。"
    body = (resp.json() or {}).get("body", "")
    if not body:
        return "日報の本文が空でした。"
    # LINE 1メッセージ上限5000字。まず入らない長さの日報はほぼ無いが保険で切る。
    return body if len(body) <= 4900 else body[:4900] + "\n…（以下省略）"


def handle_message(event):
    text = event["message"]["text"].strip()
    source_type, group_id, user_id = source_ids(event)
    reply_token = event["replyToken"]

    # /whoami・/id コマンド（グループでは通知先IDも返す）
    if text.lower() in ("/whoami", "whoami", "/id", "id"):
        if source_type in ("group", "room"):
            reply_text(
                reply_token,
                f"このトークの通知先ID（{source_type}Id）:\n{group_id}\n\n"
                f"あなたのuser_id:\n{user_id}",
            )
        else:
            reply_text(reply_token, f"あなたのuser_id:\n{user_id}")
        return

    # グループ/複数人トークでは /id 以外に反応しない（会話を邪魔しない）
    if source_type in ("group", "room"):
        return

    # 「再送」：ad-dashboardに保存された最新の日報本文をもう一度返す
    if text in RESEND_KEYWORDS:
        reply_text(reply_token, resend_daily_report())
        return

    # 未登録ユーザーチェック (USER_ZOOM_CREDENTIALSが設定されている場合のみ)
    if USER_ZOOM_CREDENTIALS and user_id not in USER_ZOOM_CREDENTIALS:
        has_fallback = all([ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET])
        if not has_fallback:
            reply_text(
                reply_token,
                f"未登録のユーザーです. 管理者に連絡してください.\nあなたのuser_id: {user_id}",
            )
            return

    dt = parse_datetime(text)
    if dt is None:
        reply_text(
            reply_token,
            "日時を認識できませんでした.\n"
            "以下の形式で入力してください:\n"
            "- 4/30 18時から\n"
            "- 明日 15時から\n"
            "- 今日 20時30分から",
        )
        return

    try:
        meeting = create_zoom_meeting(dt, user_id=user_id)
        join_url = meeting["join_url"]
        start_fmt = dt.strftime("%Y/%m/%d %H:%M")
        # ユーザー名があれば表示
        creds = USER_ZOOM_CREDENTIALS.get(user_id, {})
        name = creds.get("name", "")
        name_line = f"作成者: {name}\n" if name else ""
        reply_text(
            reply_token,
            f"Zoomミーティングを作成しました\n\n"
            f"{name_line}"
            f"開始: {start_fmt}\n"
            f"URL: {join_url}",
        )
    except requests.HTTPError as e:
        reply_text(
            reply_token,
            f"Zoomミーティングの作成に失敗しました.\nエラー: {e.response.status_code} {e.response.text}",
        )
    except Exception as e:
        reply_text(reply_token, f"エラーが発生しました.\n{str(e)}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
