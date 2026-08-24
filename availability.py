"""Googleカレンダーの空き枠を計算し、LINEにそのまま貼れる文面を作る。"""
import datetime as dt
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import zoneinfo

logger = logging.getLogger(__name__)
JST = zoneinfo.ZoneInfo("Asia/Tokyo")
WD = "月火水木金土日"

CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.getenv("GOOGLE_CALENDAR_REFRESH_TOKEN", "").strip()

# アクセストークンはインスタンス内で使い回す（有効期限1時間、60秒の余裕を見る）
_token_cache = {"value": "", "expires_at": 0.0}


class CalendarUnavailable(Exception):
    pass


def _access_token() -> str:
    if not (CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN):
        raise CalendarUnavailable("カレンダー連携が未設定です（管理者に連絡してください）。")
    if _token_cache["value"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["value"]
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        tok = json.load(urllib.request.urlopen(req, timeout=10))
    except Exception as e:
        raise CalendarUnavailable(f"Googleの認証に失敗しました。\n{e}")
    _token_cache["value"] = tok["access_token"]
    _token_cache["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    return _token_cache["value"]


def _api(path: str, params=None, body=None):
    url = "https://www.googleapis.com/calendar/v3/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + _access_token()})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(req, timeout=15))


def _busy(d0: dt.date, d1: dt.date) -> list[list[dt.datetime]]:
    """対象期間の予定を全カレンダー分まとめ、重なりを潰して返す。"""
    cals = [
        c for c in _api("users/me/calendarList").get("items", [])
        if c.get("selected", True)
        and not c.get("id", "").endswith("#holiday@group.v.calendar.google.com")
    ]
    fb = _api("freeBusy", body={
        "timeMin": dt.datetime.combine(d0, dt.time(0, 0), JST).isoformat(),
        "timeMax": dt.datetime.combine(d1 + dt.timedelta(days=1), dt.time(0, 0), JST).isoformat(),
        "timeZone": "Asia/Tokyo",
        "items": [{"id": c["id"]} for c in cals],
    })
    spans = []
    for v in fb.get("calendars", {}).values():
        for b in v.get("busy", []):
            spans.append([
                dt.datetime.fromisoformat(b["start"]).astimezone(JST),
                dt.datetime.fromisoformat(b["end"]).astimezone(JST),
            ])
    spans.sort()
    merged: list[list[dt.datetime]] = []
    for s, t in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], t)
        else:
            merged.append([s, t])
    return merged


def free_windows(d0: dt.date, d1: dt.date, first: dt.time, last: dt.time, mins: int):
    """日ごとに [(開始できる最早, 開始できる最遅)] を返す。空きが無い日は空リスト。"""
    merged = _busy(d0, d1)
    now = dt.datetime.now(JST)
    result: list[tuple[dt.date, list[tuple[dt.datetime, dt.datetime]]]] = []
    day = d0
    while day <= d1:
        win_s = dt.datetime.combine(day, first, JST)
        win_e = dt.datetime.combine(day, last, JST) + dt.timedelta(minutes=mins)
        # 今日は「今から1時間後」以降しか案内しない（直前の枠を出しても使えないため）
        cur = max(win_s, _ceil_30(now + dt.timedelta(hours=1))) if day == now.date() else win_s
        windows = []
        for s, t in merged:
            if t <= cur or s >= win_e:
                continue
            if s - cur >= dt.timedelta(minutes=mins):
                windows.append((cur, min(s, win_e)))
            cur = max(cur, t)
        if win_e - cur >= dt.timedelta(minutes=mins):
            windows.append((cur, win_e))
        slots = []
        for s, t in windows:
            latest = min(t - dt.timedelta(minutes=mins), dt.datetime.combine(day, last, JST))
            if latest >= s:
                slots.append((s, latest))
        result.append((day, slots))
        day += dt.timedelta(days=1)
    return result


def _ceil_30(d: dt.datetime) -> dt.datetime:
    """30分単位に切り上げる。"""
    d = d.replace(second=0, microsecond=0)
    add = (30 - d.minute % 30) % 30
    return d + dt.timedelta(minutes=add)


def suggest_text(d0: dt.date, d1: dt.date, first: dt.time, last: dt.time,
                 mins: int, count: int = 3) -> str:
    """相手にそのまま送れる候補文を作る。日をばらけさせて count 件選ぶ。"""
    days = free_windows(d0, d1, first, last, mins)
    picks: list[dt.datetime] = []
    for _, slots in days:
        if not slots:
            continue
        s, latest = slots[0]
        # 半端な開始時刻は避け、30分単位に寄せる
        cand = _ceil_30(s)
        picks.append(cand if cand <= latest else s)
        if len(picks) >= count:
            break
    if not picks:
        return (f"{d0.month}/{d0.day}〜{d1.month}/{d1.day} は "
                f"{first.strftime('%H:%M')}〜{last.strftime('%H:%M')}スタートで"
                f"{mins}分の空きがありませんでした。")
    lines = [f"・{p.month}/{p.day}({WD[p.weekday()]}) {p.strftime('%H:%M')}〜" for p in picks]
    return ("下記のいずれかでご都合いかがでしょうか？\n\n"
            + "\n".join(lines)
            + "\n\nどれも難しければ、他の日程も出しますのでお気軽にお申し付けください！")


def detail_text(d0: dt.date, d1: dt.date, first: dt.time, last: dt.time, mins: int) -> str:
    """自分用の詳細（日ごとの空き帯）。"""
    out = []
    for day, slots in free_windows(d0, d1, first, last, mins):
        label = f"{day.month}/{day.day}({WD[day.weekday()]})"
        if not slots:
            out.append(f"{label} 空きなし")
        else:
            out.append(f"{label} " + " / ".join(
                f"{s.strftime('%H:%M')}" + (f"〜{t.strftime('%H:%M')}" if t > s else "")
                for s, t in slots))
    return "\n".join(out)
