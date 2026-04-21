import os
import re
from datetime import datetime, timedelta

import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

load_dotenv()

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

JST = pytz.timezone("Asia/Tokyo")


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


def get_zoom_access_token() -> str:
    url = (
        f"https://zoom.us/oauth/token"
        f"?grant_type=account_credentials&account_id={ZOOM_ACCOUNT_ID}"
    )
    resp = requests.post(url, auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET), timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_zoom_meeting(start_time: datetime, duration: int = 60) -> dict:
    token = get_zoom_access_token()
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


def reply_text(reply_token: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()

    dt = parse_datetime(text)
    if dt is None:
        reply_text(
            event.reply_token,
            "日時を認識できませんでした。\n"
            "以下の形式で入力してください：\n"
            "・「4/30 18時から」\n"
            "・「明日 15時から」\n"
            "・「今日 20時30分から」",
        )
        return

    try:
        meeting = create_zoom_meeting(dt)
        join_url = meeting["join_url"]
        start_fmt = dt.strftime("%Y年%m月%d日 %H:%M")
        reply_text(
            event.reply_token,
            f"Zoomミーティングを作成しました！\n\n"
            f"📅 開始: {start_fmt}\n"
            f"🔗 {join_url}",
        )
    except requests.HTTPError as e:
        reply_text(
            event.reply_token,
            f"Zoomミーティングの作成に失敗しました。\nエラー: {e.response.status_code} {e.response.text}",
        )
    except Exception as e:
        reply_text(event.reply_token, f"エラーが発生しました。\n{str(e)}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
