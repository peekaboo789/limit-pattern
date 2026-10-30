"""涨停前的 K 线不够时，按需向行情源补几天。只进计算表，不改正式行情。"""

from __future__ import annotations

import threading
from datetime import date, timedelta

from backend.app.algorithms.features import Bar
from backend.app.models.tables import CalcBar, DailyQuote

_LOCK = threading.Lock()
_MEMORY: dict[tuple[str, date], dict] = {}
_TRIED: set[tuple[str, date]] = set()
# 特征窗口 3 根 + 5 日量比基准
WANTED_PRIOR_BARS = 8


def clear_calc_memory() -> None:
    with _LOCK:
        _MEMORY.clear()
        _TRIED.clear()


def _to_bar(day: date, row) -> Bar | None:
    if None in (row.open, row.high, row.low, row.close):
        return None
    return Bar(
        trade_date=day,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        pre_close=row.pre_close,
        volume=row.volume,
        amount=row.amount,
        turnover_rate=row.turnover_rate,
    )


from collections import namedtuple
from contextlib import contextmanager

_Row = namedtuple("_Row", "open high low close pre_close volume amount turnover_rate")
_COLS = ("open", "high", "low", "close", "pre_close", "volume", "amount", "turnover_rate")


@contextmanager
def bulk_bars(session, as_of: date):
    """扫描全市场前一次性把 ≤as_of 的日线读进内存（两条查询），避免每只股票各查一次。"""
    if session.info.get("lp_bulk") is not None:
        yield
        return
    data: dict[str, dict[date, Bar]] = {}
    for table in (CalcBar, DailyQuote):  # 正式行情后放，覆盖计算表
        columns = [table.stock_code, table.trade_date] + [getattr(table, name) for name in _COLS]
        for row in session.query(*columns).filter(table.trade_date <= as_of):
            bar = _to_bar(row[1], _Row(*row[2:]))
            if bar is not None:
                data.setdefault(row[0], {})[row[1]] = bar
    session.info["lp_bulk"] = (as_of, data)
    try:
        yield
    finally:
        session.info.pop("lp_bulk", None)
        session.info.pop("lp_partials", None)


def _forget_bulk(session, code: str) -> None:
    bulk = session.info.get("lp_bulk")
    if bulk is not None:
        bulk[1].pop(code, None)


PARTIAL_SOURCES = ("intraday", "morning_freeze")


def _partials(session) -> set:
    """盘中半天线（临时日线）的 (代码, 日期)：不占「上市满 20 个完整交易日」的名额。"""
    cached = session.info.get("lp_partials")
    if cached is not None:
        return cached
    found = {
        (code, day)
        for code, day in session.query(DailyQuote.stock_code, DailyQuote.trade_date).filter(DailyQuote.source.in_(PARTIAL_SOURCES))
    }
    if session.info.get("lp_bulk") is not None:
        session.info["lp_partials"] = found
    return found


def load_bars(session, code: str, as_of: date) -> list[Bar]:
    bars = _load_bars(session, code, as_of)
    partial = _partials(session)
    if partial:
        for bar in bars:
            if (code, bar.trade_date) in partial:
                bar.is_partial = True
    return bars


def _load_bars(session, code: str, as_of: date) -> list[Bar]:
    """正式行情优先。晚于 as_of 的一律不要。"""
    bulk = session.info.get("lp_bulk")
    if bulk is not None and as_of <= bulk[0] and code in bulk[1]:
        merged_bulk = {d: b for d, b in bulk[1][code].items() if d <= as_of}
        with _LOCK:
            for (cached_code, cached_day), payload in _MEMORY.items():
                if cached_code == code and cached_day <= as_of and cached_day not in merged_bulk:
                    bar = _to_bar(cached_day, _DictRow(payload))
                    if bar is not None:
                        merged_bulk[cached_day] = bar
        return [merged_bulk[day] for day in sorted(merged_bulk)]
    merged: dict[date, Bar] = {}
    calc_rows = (
        session.query(CalcBar)
        .filter(CalcBar.stock_code == code, CalcBar.trade_date <= as_of)
        .all()
    )
    for row in calc_rows:
        bar = _to_bar(row.trade_date, row)
        if bar is not None:
            merged[row.trade_date] = bar
    with _LOCK:
        for (cached_code, cached_day), payload in _MEMORY.items():
            if cached_code == code and cached_day <= as_of:
                bar = _to_bar(cached_day, _DictRow(payload))
                if bar is not None and cached_day not in merged:
                    merged[cached_day] = bar
    official = (
        session.query(DailyQuote)
        .filter(DailyQuote.stock_code == code, DailyQuote.trade_date <= as_of)
        .all()
    )
    for row in official:
        bar = _to_bar(row.trade_date, row)
        if bar is not None:
            merged[row.trade_date] = bar
    return [merged[day] for day in sorted(merged)]


class _DictRow:
    def __init__(self, payload: dict) -> None:
        self.open = payload.get("open")
        self.high = payload.get("high")
        self.low = payload.get("low")
        self.close = payload.get("close")
        self.pre_close = payload.get("pre_close")
        self.volume = payload.get("volume")
        self.amount = payload.get("amount")
        self.turnover_rate = payload.get("turnover_rate")


def _store(session, code: str, bars: list[dict], as_of: date) -> int:
    kept = 0
    for item in bars:
        day = item.get("trade_date")
        if not isinstance(day, date) or day > as_of:
            continue
        if None in (item.get("open"), item.get("high"), item.get("low"), item.get("close")):
            continue
        row = (
            session.query(CalcBar)
            .filter(CalcBar.stock_code == code, CalcBar.trade_date == day)
            .one_or_none()
        )
        if row is None:
            row = CalcBar(stock_code=code, trade_date=day)
            session.add(row)
        row.open = item.get("open")
        row.high = item.get("high")
        row.low = item.get("low")
        row.close = item.get("close")
        row.pre_close = item.get("pre_close")
        row.volume = item.get("volume")
        row.amount = item.get("amount")
        row.turnover_rate = item.get("turnover_rate")
        with _LOCK:
            _MEMORY[(code, day)] = {
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "pre_close": row.pre_close,
                "volume": row.volume,
                "amount": row.amount,
                "turnover_rate": row.turnover_rate,
            }
        kept += 1
    session.flush()
    return kept


def ensure_prior_bars(session, code: str, limit_date: date, as_of: date) -> str | None:
    """让涨停日之前有 8 根 K 线（3 根特征 + 5 日基准）；至少要 3 根。成功返回 None，失败返回可展示的原因。"""
    if limit_date > as_of:
        return "不能使用未来行情"
    cap = min(limit_date, as_of)
    prior = [bar for bar in load_bars(session, code, cap) if bar.trade_date < limit_date]
    if len(prior) >= WANTED_PRIOR_BARS or (len(prior) >= 3 and (code, limit_date) in _TRIED):
        return None
    if session.info.get("lp_no_backfill"):
        return None if len(prior) >= 3 else "回测时不联网补拉，涨停前K线不足"
    with _LOCK:
        _TRIED.add((code, limit_date))
    start = limit_date - timedelta(days=30)
    end = limit_date - timedelta(days=1)
    if end > as_of:
        end = as_of
    if end < start:
        return "没有可补的历史区间"
    try:
        from backend.app.services.sync_service import fetch_kline_retry

        fetched = fetch_kline_retry(code, start, end)
    except Exception:
        return "补拉历史日线失败"
    usable = []
    for item in fetched or []:
        day = item.get("trade_date")
        if isinstance(day, date) and day < limit_date and day <= as_of:
            usable.append(item)
    _store(session, code, usable, as_of)
    _forget_bulk(session, code)
    prior = [bar for bar in load_bars(session, code, cap) if bar.trade_date < limit_date]
    if len(prior) >= 3:
        return None
    return "补拉之后仍然凑不齐涨停前三根K线"
