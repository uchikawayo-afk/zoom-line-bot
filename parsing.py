"""LINEの自由文から「いつ・何分」を読み取る。"""
import datetime as dt
import re
import zoneinfo

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
WD_CHARS = "月火水木金土日"

_REL_DAYS = {"今日": 0, "本日": 0, "きょう": 0, "明日": 1, "あした": 1, "あす": 1,
             "明後日": 2, "あさって": 2, "明々後日": 3, "しあさって": 3}

# 日付パターンは上から順に試す。(正規表現, 解決関数) の組。
_DATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(\d{1,2})\s*[/／]\s*(\d{1,2})"), "md"),
    (re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "md"),
    (re.compile(r"(今日|本日|きょう|明後日|あさって|明々後日|しあさって|明日|あした|あす)"), "rel"),
    (re.compile(rf"(今週|来週|再来週)?\s*([{WD_CHARS}])\s*曜日?"), "wd"),
    (re.compile(r"(\d{1,2})\s*日(?!\s*間)"), "d"),
]

_TIME_PATTERNS = [
    re.compile(r"(午前|午後|朝|夜|夕方)?\s*(\d{1,2})\s*[:：]\s*(\d{2})"),
    re.compile(r"(午前|午後|朝|夜|夕方)?\s*(\d{1,2})\s*時\s*(半)"),
    re.compile(r"(午前|午後|朝|夜|夕方)?\s*(\d{1,2})\s*時\s*([0-5]?\d)\s*分"),
    re.compile(r"(午前|午後|朝|夜|夕方)?\s*(\d{1,2})\s*時(?!\s*間)"),
]

_DURATION_PATTERNS = [
    (re.compile(r"(\d+)\s*時間\s*半"), lambda m: int(m.group(1)) * 60 + 30),
    (re.compile(r"(\d+(?:[.．]\d+)?)\s*時間"), lambda m: int(float(m.group(1).replace("．", ".")) * 60)),
    (re.compile(r"(\d+)\s*分"), lambda m: int(m.group(1))),
    (re.compile(r"半日"), lambda m: 240),
]


def _resolve_date(kind: str, m: re.Match, now: dt.datetime) -> dt.date | None:
    if kind == "md":
        month, day = int(m.group(1)), int(m.group(2))
        for year in (now.year, now.year + 1):
            try:
                d = dt.date(year, month, day)
            except ValueError:
                return None
            if d >= now.date():
                return d
        return None
    if kind == "rel":
        return now.date() + dt.timedelta(days=_REL_DAYS[m.group(1)])
    if kind == "wd":
        week, ch = m.group(1), m.group(2)
        target = WD_CHARS.index(ch)
        if week in ("来週", "再来週"):
            offset = 7 if week == "来週" else 14
            monday = now.date() + dt.timedelta(days=offset - now.weekday())
            return monday + dt.timedelta(days=target)
        delta = (target - now.weekday()) % 7
        if delta == 0:
            delta = 7  # 「火曜」だけなら今日ではなく次の火曜
        return now.date() + dt.timedelta(days=delta)
    if kind == "d":
        day = int(m.group(1))
        for month_offset in (0, 1):
            month = (now.month - 1 + month_offset) % 12 + 1
            year = now.year + (1 if now.month + month_offset > 12 else 0)
            try:
                d = dt.date(year, month, day)
            except ValueError:
                continue
            if d >= now.date():
                return d
        return None
    return None


def _resolve_time(m: re.Match) -> tuple[int, int]:
    ampm, hour = m.group(1), int(m.group(2))
    tail = m.group(3) if m.lastindex and m.lastindex >= 3 else None
    minute = 30 if tail == "半" else (int(tail) if tail and tail.isdigit() else 0)
    if ampm in ("午後", "夜", "夕方") and hour < 12:
        hour += 12
    elif ampm in ("午前", "朝") and hour == 12:
        hour = 0
    return hour, minute


def parse_request(text: str, now: dt.datetime | None = None) -> tuple[dt.datetime, int] | None:
    """(開始日時, 所要分) を返す。読み取れなければ None。"""
    now = now or dt.datetime.now(JST)
    rest = text
    date = None
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.search(rest)
        if m:
            date = _resolve_date(kind, m, now)
            if date is None:
                return None
            rest = rest[:m.start()] + " " + rest[m.end():]
            break

    time_hm = None
    for pattern in _TIME_PATTERNS:
        m = pattern.search(rest)
        if m:
            time_hm = _resolve_time(m)
            rest = rest[:m.start()] + " " + rest[m.end():]
            break

    if time_hm is None:
        return None
    hour, minute = time_hm
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    if date is None:
        # 日付が無ければ今日。すでに過ぎていれば明日として扱う。
        date = now.date()
        if dt.datetime.combine(date, dt.time(hour, minute), JST) < now:
            date += dt.timedelta(days=1)

    duration = 60
    for pattern, fn in _DURATION_PATTERNS:
        m = pattern.search(rest)
        if m:
            value = fn(m)
            if 5 <= value <= 480:
                duration = value
            break

    return dt.datetime.combine(date, dt.time(hour, minute), JST), duration


def parse_availability(text: str, now: dt.datetime | None = None):
    """「空き」系メッセージから (開始日, 終了日, 最早開始, 最遅開始, 所要分) を返す。"""
    now = now or dt.datetime.now(JST)
    today = now.date()

    if "来週" in text:
        d0 = today + dt.timedelta(days=7 - today.weekday())
        d1 = d0 + dt.timedelta(days=6)
    elif "再来週" in text:
        d0 = today + dt.timedelta(days=14 - today.weekday())
        d1 = d0 + dt.timedelta(days=6)
    elif "今週" in text:
        d0, d1 = today, today + dt.timedelta(days=6 - today.weekday())
    else:
        d0, d1 = today, today + dt.timedelta(days=6)

    # 「11-17」「11時〜17時」のような開始可能レンジ指定
    first, last = dt.time(11, 0), dt.time(17, 0)
    m = re.search(r"(\d{1,2})\s*時?\s*[-〜~ー–]\s*(\d{1,2})\s*時?", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 <= a < b <= 23:
            first, last = dt.time(a, 0), dt.time(b, 0)

    mins = 60
    body = re.sub(r"(\d{1,2})\s*時?\s*[-〜~ー–]\s*(\d{1,2})\s*時?", " ", text)
    for pattern, fn in _DURATION_PATTERNS:
        mm = pattern.search(body)
        if mm:
            value = fn(mm)
            if 5 <= value <= 480:
                mins = value
            break

    return d0, d1, first, last, mins


def parse_date_only(text: str, now: dt.datetime | None = None) -> dt.date | None:
    """日付だけが書かれている（時刻が無い）場合にその日付を返す。"""
    now = now or dt.datetime.now(JST)
    for pattern, kind in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        rest = text[:m.start()] + " " + text[m.end():]
        if any(p.search(rest) for p in _TIME_PATTERNS):
            return None
        return _resolve_date(kind, m, now)
    return None
