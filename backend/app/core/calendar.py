"""A 股交易日历（沪深交易所休市安排）。

2025、2026 年已于 2026-10-30 逐条核对官方公告，无需更正：
- 2025：上证公告〔2024〕38号《关于上海证券交易所2025年部分节假日休市安排的通知》（2024-12-23，
  http://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml），依据国办发明电〔2024〕12号。
- 2026：上证公告〔2025〕45号《关于上海证券交易所2026年部分节假日休市安排的通知》（2025-12-22，
  http://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml），
  深交所依据证监办发〔2025〕130号发布相同安排（如春节 2/15–2/23 休市）。
2027 年公告尚未发布，先按节日推算，标为 estimated；收盘后若某个“交易日”全市场没有日线，
自动记入数据目录 holidays_auto.json（推算年份）或只告警（已公告年份）。
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


SOURCES = {
    2025: "上证公告〔2024〕38号 http://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml",
    2026: "上证公告〔2025〕45号 http://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml",
    2027: "推算（交易所公告未发布）",
}


def _read_override(name: str) -> tuple[set[date], set[date]]:
    from backend.app.core.config import data_dir

    path = data_dir() / name
    if not path.is_file():
        return set(), set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        closed = {date.fromisoformat(x) for x in payload.get("closed", [])}
        opened = {date.fromisoformat(x) for x in payload.get("open", [])}
        return closed, opened
    except (OSError, ValueError, AttributeError):
        return set(), set()


def _overrides() -> tuple[set[date], set[date]]:
    closed, opened = _read_override("holidays.json")
    auto_closed, _ = _read_override("holidays_auto.json")
    return closed | auto_closed, opened


def observe_session(day: date, market_bars: int) -> dict | None:
    """收盘后核对：日历说开市但全市场 0 根日线 → 推算年份记成休市，已公告年份只告警。"""
    import logging

    if market_bars > 0 or not is_trading_day(day):
        return None
    log = logging.getLogger("limit_pattern.calendar")
    if day.year in ESTIMATED_YEARS or day.year not in HOLIDAYS:
        from backend.app.core.config import data_dir

        path = data_dir() / "holidays_auto.json"
        closed, _ = _read_override("holidays_auto.json")
        closed.add(day)
        path.write_text(json.dumps({"closed": sorted(d.isoformat() for d in closed)}, ensure_ascii=False, indent=1), encoding="utf-8")
        log.warning("%s 日历记为交易日但没有任何日线，已按休市记入 holidays_auto.json", day)
        return {"day": day.isoformat(), "action": "marked_closed"}
    log.warning("%s 日历记为交易日但没有任何日线，请检查数据源或交易所公告", day)
    return {"day": day.isoformat(), "action": "warned"}


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
        "source": SOURCES.get(year, ""),
        "auto_closed": sorted(d.isoformat() for d in _read_override("holidays_auto.json")[0] if d.year == year),
        "holidays": sorted(HOLIDAYS.get(year, [])),
    }
