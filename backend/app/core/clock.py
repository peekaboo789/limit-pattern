"""全项目唯一的时间来源。时区写死东八区，不读系统时区。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_CLOSE = time(15, 0)


def now() -> datetime:
    return datetime.now(SHANGHAI)


def today() -> date:
    return now().date()


def session_phase(moment: datetime | None = None) -> str:
    """pre_open / morning / frozen。11:30 起算 frozen，下午的行情不再进形态窗口。"""
    moment = moment or now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    else:
        moment = moment.astimezone(SHANGHAI)
    clock = moment.time()
    if clock < MORNING_OPEN:
        return "pre_open"
    if clock < MORNING_CLOSE:
        return "morning"
    return "frozen"


def morning_ended(moment: datetime | None = None) -> bool:
    return session_phase(moment) == "frozen"


def in_session(moment: datetime | None = None) -> bool:
    moment = moment or now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    else:
        moment = moment.astimezone(SHANGHAI)
    clock = moment.time()
    if MORNING_OPEN <= clock < MORNING_CLOSE:
        return True
    return time(13, 0) <= clock < AFTERNOON_CLOSE
