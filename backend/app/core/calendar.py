"""A 股交易日历（沪深交易所休市安排）。

2025、2026 年按交易所公告整理；2027 年公告尚未发布，先按节日推算，标为 estimated。
数据目录下 holidays.json 可以覆盖：{"closed": ["2027-02-08", ...], "open": ["..."]}，
open 用于把误列的日子改回交易日。周末一律休市（A 股调休的周末也不开市）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from functools import lru_cache

from backend.app.core.clock import SHANGHAI, in_session, now


def _span(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    days = []
    while first <= last:
        days.append(first.isoformat())
        first += timedelta(days=1)
    return days


HOLIDAYS: dict[int, list[str]] = {
    2025: [
        "2025-01-01",
        *_span("2025-01-28", "2025-02-04"),
        "2025-04-04",
        *_span("2025-05-01", "2025-05-05"),
        "2025-06-02",
        *_span("2025-10-01", "2025-10-08"),
    ],
    2026: [
        *_span("2026-01-01", "2026-01-02"),
        *_span("2026-02-16", "2026-02-23"),
        "2026-04-06",
        *_span("2026-05-01", "2026-05-05"),
        "2026-06-19",
        "2026-09-25",
        *_span("2026-10-01", "2026-10-07"),
    ],
    # 推算：交易所公告发布后请核对并改 holidays.json 或这里。
    2027: [
        "2027-01-01",
        *_span("2027-02-05", "2027-02-12"),
        "2027-04-05",
        *_span("2027-05-03", "2027-05-05"),
        "2027-06-09",
        "2027-09-15",
        *_span("2027-10-01", "2027-10-07"),
    ],
}
ESTIMATED_YEARS = {2027}


def _overrides() -> tuple[set[date], set[date]]:
    from backend.app.core.config import data_dir

    path = data_dir() / "holidays.json"
    if not path.is_file():
        return set(), set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        closed = {date.fromisoformat(x) for x in payload.get("closed", [])}
        opened = {date.fromisoformat(x) for x in payload.get("open", [])}
        return closed, opened
    except (OSError, ValueError, AttributeError):
        return set(), set()


@lru_cache(maxsize=1)
def _builtin() -> frozenset[date]:
    return frozenset(date.fromisoformat(day) for days in HOLIDAYS.values() for day in days)


def is_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    closed, opened = _overrides()
    if day in opened:
        return True
    return day not in _builtin() and day not in closed


def previous_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    for _ in range(40):
        if is_trading_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return cursor


def next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    for _ in range(40):
        if is_trading_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    return cursor


def market_open(moment: datetime | None = None) -> bool:
    moment = moment or now()
    moment = moment.astimezone(SHANGHAI) if moment.tzinfo else moment.replace(tzinfo=SHANGHAI)
    return is_trading_day(moment.date()) and in_session(moment)


def calendar_info(year: int) -> dict:
    return {
        "year": year,
        "estimated": year in ESTIMATED_YEARS,
        "holidays": sorted(HOLIDAYS.get(year, [])),
    }
