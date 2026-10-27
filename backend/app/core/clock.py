"""全项目唯一的时间来源。时区写死东八区，不读系统时区。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_CLOSE = time(15, 0)


def now() -> datetime:
    return datetime.now(SHANGHAI)


def now_naive() -> datetime:
    """写进不带时区的列（`created_at` 那一类）时用这个。

    列里没有偏移，页面按浏览器本地时区（东八区）读它：存 UTC 的话，回测列表那一批
    时间一律早八小时，而质量检查那一格和它差一天。全项目只认东八区挂钟，
    存的时候就把时区摘掉，别让两种挂钟住在同一张表里。
    """
    return now().replace(tzinfo=None)


def today() -> date:
    return now().date()


def _shanghai(moment: datetime) -> datetime:
    """把外面递进来的时间统一成东八区。裸时间戳按东八区读，带时区的换算过去。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=SHANGHAI)
    return moment.astimezone(SHANGHAI)


def session_phase(moment: datetime | None = None) -> str:
    """pre_open / morning / frozen。11:30 起算 frozen，下午的行情不再进形态窗口。"""
    moment = _shanghai(moment or now())
    clock = moment.time()
    if clock < MORNING_OPEN:
        return "pre_open"
    if clock < MORNING_CLOSE:
        return "morning"
    return "frozen"


def morning_ended(moment: datetime | None = None) -> bool:
    return session_phase(moment) == "frozen"


def settled_day(moment: datetime | None = None) -> date:
    """最新一根**已经收盘、价格不会再动**的日线属于哪一天。

    15:00 之后当天才算落定；15:00 之前（开盘前、盘中、午休都算）当天那根还是半根，
    把它当成一个完整交易日喂进三天形态窗口，等于把半天当成一整天来数。
    所以「追赶同步补到哪一天」「自动补齐算到哪一天」都以这天为上限。
    """
    moment = _shanghai(moment or now())
    if moment.time() >= AFTERNOON_CLOSE:
        return moment.date()
    return moment.date() - timedelta(days=1)


def in_session(moment: datetime | None = None) -> bool:
    moment = _shanghai(moment or now())
    clock = moment.time()
    if MORNING_OPEN <= clock < MORNING_CLOSE:
        return True
    return time(13, 0) <= clock < AFTERNOON_CLOSE
