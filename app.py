import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import zoneinfo

import requests
from flask import Flask, abort, request

import availability
import parsing

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def sanitize_env(value: str) -> str:
    """環境変数からASCII以外の文字と前後の空白を除去"""
    return value.strip().encode("ascii", errors="ignore").decode("ascii")


LINE_CHANNEL_SECRET = sanitize_env(os.getenv("LINE_CHANNEL_SECRET", ""))
LINE_CHANNEL_ACCESS_TOKEN = sanitize_env(os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))
DAILY_REPORT_LATEST_URL = sanitize_env(os.getenv(
    "DAILY_REPORT_LATEST_URL",
    "https://ad-dashboard-27531714336.asia-northeast1.run.app/api/daily-report/latest",
))
DAILY_REPORT_SECRET = sanitize_env(os.getenv("DAILY_REPORT_SECRET", ""))
RESEND_KEYWORDS = ("再送", "日報", "日報再送", "にっぽう")
HELP_KEYWORDS = ("ヘルプ", "help", "使い方", "つかいかた", "?", "？")
AVAILABILITY_KEYWORDS = ("空き", "あき", "空", "予定", "空き時間", "空いてる")
ZOOM_ACCOUNT_ID = sanitize_env(os.getenv("ZOOM_ACCOUNT_ID", ""))
ZOOM_CLIENT_ID = sanitize_env(os.getenv("ZOOM_CLIENT_ID", ""))
ZOOM_CLIENT_SECRET = sanitize_env(os.getenv("ZOOM_CLIENT_SECRET", ""))

_raw_creds = os.getenv("USER_ZOOM_CREDENTIALS", "")
try:
    USER_ZOOM_CREDENTIALS: dict = json.loads(_raw_creds) if _raw_creds else {}
    if USER_ZOOM_CREDENTIALS:
        logger.info(f"Loaded Zoom credentials for {len(USER_ZOOM_CREDENTIALS)} user(s)")
except (json.JSONDecodeError, TypeError) as e:
    logger.error(f"USER_ZOOM_CREDENTIALS parse error: {e}")
    USER_ZOOM_CREDENTIALS = {}

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
APP_VERSION = "2026-08-24-cloudrun-calendar"

HELP_TEXT = (
    "できること\n"
    "\n"
    "■ Zoomリンクを作る\n"
    "日時を送るだけです。\n"
    "・8/25 14時から\n"
    "・明日 15時 90分\n"
    "・来週火曜 14時半\n"
    "・今日 20:30\n"
    "所要時間を書かなければ60分になります。\n"
    "\n"
    "■ 空いている日程を出す\n"
    "・空き　　　　→ 今日から1週間\n"
    "・空き 来週\n"
    "・空き 今週 10-18　→ 10〜18時スタートで探す\n"
    "・空き 90分\n"
    "そのまま相手に送れる候補文で返します。\n"
    "「空き 詳しく」で日ごとの空き帯を全部出します。\n"
    "\n"
    "■ その他\n"
    "・再送　→ 最新の日報をもう一度\n"
    "・/id　 → user_id / groupId"
)


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
    resp = requests.post(url, auth=(client_id, client_secret), timeout=8)
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_zoom_meeting(start_time: dt.datetime, user_id: str = "", duration: int = 60) -> dict:
    token = get_zoom_access_token(user_id)
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
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=8,
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


def _line_post(url: str, payload: dict) -> requests.Response:
    return requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=10,
    )


def reply_text(reply_token: str, text: str, user_id: str = "") -> None:
    """まずreplyで返し、replyTokenが切れていたらpushで送り直す。

    コールドスタートや外部API待ちでreplyTokenが失効しても無言にならないようにする。
    """
    messages = [{"type": "text", "text": text}]
    resp = _line_post(LINE_REPLY_URL, {"replyToken": reply_token, "messages": messages})
    if resp.status_code == 200:
        return
    logger.error(f"LINE reply failed: {resp.status_code} {resp.text}")
    if user_id:
        push = _line_post(LINE_PUSH_URL, {"to": user_id, "messages": messages})
        if push.status_code != 200:
            logger.error(f"LINE push failed: {push.status_code} {push.text}")
        else:
            logger.info("recovered via push")


# /healthz は run.app のフロントエンドに横取りされてアプリまで届かないため /health を使う
@app.route("/health", methods=["GET"])
@app.route("/healthz", methods=["GET"])
def healthz():
    return {"ok": True, "version": APP_VERSION}


# 直近に見たグループ/複数人トークのID（通知先の設定用。プロセス再起動で消える）
SEEN_SOURCES: list[dict] = []


def remember_source(stype: str, sid: str, event_type: str) -> None:
    if not sid:
        return
    SEEN_SOURCES[:] = [s for s in SEEN_SOURCES if s["id"] != sid]
    SEEN_SOURCES.insert(0, {"type": stype, "id": sid, "event": event_type})
    del SEEN_SOURCES[5:]
    logger.info(f"source seen: {stype} {sid} ({event_type})")


@app.route("/last-group", methods=["GET"])
def last_group():
    """招待直後に通知先IDを拾うための口。?k=<LAST_GROUP_KEY> が必要。"""
    key = sanitize_env(os.getenv("LAST_GROUP_KEY", "sponsor-setup"))
    if request.args.get("k", "") != key:
        abort(404)
    return {"sources": SEEN_SOURCES}


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
        stype, gid, _ = source_ids(event)
        if stype in ("group", "room"):
            remember_source(stype, gid, etype)
        try:
            if etype == "join":
                handle_join(event)
            elif etype == "message" and event.get("message", {}).get("type") == "text":
                handle_message(event)
        except Exception:
            # 1件の失敗で残りのイベントを落とさない
            logger.exception("event handling failed")
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
    """ad-dashboard から最新の日報本文を取得して返す。取得失敗時は理由を返す。"""
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
    return body if len(body) <= 4900 else body[:4900] + "\n…（以下省略）"


def handle_availability(text: str) -> str:
    d0, d1, first, last, mins = parsing.parse_availability(text)
    try:
        if any(k in text for k in ("詳しく", "詳細", "全部", "一覧")):
            head = (f"{d0.month}/{d0.day}〜{d1.month}/{d1.day}　"
                    f"{first.strftime('%H:%M')}〜{last.strftime('%H:%M')}スタート／{mins}分\n\n")
            return head + availability.detail_text(d0, d1, first, last, mins)
        return availability.suggest_text(d0, d1, first, last, mins)
    except availability.CalendarUnavailable as e:
        return str(e)
    except Exception as e:
        logger.exception("availability failed")
        return f"カレンダーの取得に失敗しました。\n{e}"


def day_slots_text(day: dt.date) -> str:
    """日付だけ送られたとき、その日の空きを返す。"""
    try:
        detail = availability.detail_text(day, day, dt.time(9, 0), dt.time(20, 0), 60)
    except Exception:
        return ""
    return (f"{day.month}/{day.day} の空き（9:00〜20:00スタート／60分）\n{detail}\n\n"
            "時刻まで送っていただければZoomを作ります（例: "
            f"{day.month}/{day.day} 14時）")


def handle_message(event):
    text = event["message"]["text"].strip()
    source_type, group_id, user_id = source_ids(event)
    reply_token = event["replyToken"]

    def say(msg: str) -> None:
        reply_text(reply_token, msg, user_id=user_id if source_type == "user" else "")

    if text.lower() in ("/whoami", "whoami", "/id", "id"):
        if source_type in ("group", "room"):
            say(f"このトークの通知先ID（{source_type}Id）:\n{group_id}\n\n"
                f"あなたのuser_id:\n{user_id}")
        else:
            say(f"あなたのuser_id:\n{user_id}")
        return

    # グループ/複数人トークでは /id 以外に反応しない（会話を邪魔しない）
    if source_type in ("group", "room"):
        return

    if text.lower() in HELP_KEYWORDS:
        say(HELP_TEXT)
        return

    if text in RESEND_KEYWORDS:
        say(resend_daily_report())
        return

    if any(text.startswith(k) for k in AVAILABILITY_KEYWORDS):
        say(handle_availability(text))
        return

    parsed = parsing.parse_request(text)
    if parsed is None:
        # 日付だけ送られた場合は、その日の空きを返して次の一手を示す
        day = parsing.parse_date_only(text)
        if day is not None:
            hint = day_slots_text(day)
            if hint:
                say(hint)
                return
        say("日時を認識できませんでした。\n\n" + HELP_TEXT)
        return

    start, duration = parsed
    if USER_ZOOM_CREDENTIALS and user_id not in USER_ZOOM_CREDENTIALS:
        if not all([ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET]):
            say(f"未登録のユーザーです。管理者に連絡してください。\nあなたのuser_id: {user_id}")
            return

    try:
        meeting = create_zoom_meeting(start, user_id=user_id, duration=duration)
        name = USER_ZOOM_CREDENTIALS.get(user_id, {}).get("name", "")
        name_line = f"作成者: {name}\n" if name else ""
        wd = "月火水木金土日"[start.weekday()]
        say(f"Zoomミーティングを作成しました\n\n"
            f"{name_line}"
            f"開始: {start.strftime('%Y/%m/%d')}({wd}) {start.strftime('%H:%M')}〜"
            f"（{duration}分）\n"
            f"URL: {meeting['join_url']}")
    except requests.HTTPError as e:
        say(f"Zoomミーティングの作成に失敗しました。\n"
            f"エラー: {e.response.status_code} {e.response.text[:300]}")
    except Exception as e:
        logger.exception("zoom creation failed")
        say(f"エラーが発生しました。\n{e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
