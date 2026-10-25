"""涨停前的 K 线不够时，按需向行情源补几天。只进计算表，不改正式行情。"""

from __future__ import annotations

import threading
from datetime import date, timedelta

from backend.app.algorithms.features import Bar
from backend.app.models.tables import CalcBar, DailyQuote

_LOCK = threading.Lock()
_MEMORY: dict[tuple[str, date], dict] = {}


def clear_calc_memory() -> None:
    with _LOCK:
        _MEMORY.clear()


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


def load_bars(session, code: str, as_of: date) -> list[Bar]:
    """正式行情优先。晚于 as_of 的一律不要。"""
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


def load_bars_bulk(session, as_of: date, codes: list[str] | None = None) -> dict[str, list[Bar]]:
    """一次把全市场的历史读进内存，按代码分组。口径与 `load_bars` 完全一致。

    盘中全市场一轮要给四千多只股票建窗口。逐只走 `load_bars` 是每次三轮查询、
    一万多次往返；这里改成三轮查询取全部，再由调用方在内存里配。
    `codes` 给了就只取那批（快刷的榜前 N），不给就取全表。
    """
    wanted = set(codes) if codes is not None else None
    grouped: dict[str, dict[date, Bar]] = {}

    def _put(code: str, day: date, bar: Bar, override: bool) -> None:
        if wanted is not None and code not in wanted:
            return
        if bar.trade_date > as_of:
            return
        slot = grouped.setdefault(code, {})
        if override or day not in slot:
            slot[day] = bar

    calc_rows = session.query(CalcBar).filter(CalcBar.trade_date <= as_of)
    if wanted is not None:
        calc_rows = calc_rows.filter(CalcBar.stock_code.in_(list(wanted)))
    for row in calc_rows.all():
        bar = _to_bar(row.trade_date, row)
        if bar is not None:
            _put(row.stock_code, row.trade_date, bar, False)
    with _LOCK:
        for (code, day), payload in _MEMORY.items():
            bar = _to_bar(day, _DictRow(payload))
            if bar is not None:
                _put(code, day, bar, False)
    official = session.query(DailyQuote).filter(DailyQuote.trade_date <= as_of)
    if wanted is not None:
        official = official.filter(DailyQuote.stock_code.in_(list(wanted)))
    for row in official.all():
        bar = _to_bar(row.trade_date, row)
        if bar is not None:
            # 正式行情盖住补来的线，和 `load_bars` 的优先级一样。
            _put(row.stock_code, row.trade_date, bar, True)
    return {code: [slot[day] for day in sorted(slot)] for code, slot in grouped.items()}


def _fetch_kline(code: str, start: date, end: date) -> list[dict]:
    from backend.app.services.sync_service import fetch_kline_retry

    return fetch_kline_retry(code, start, end)


def ensure_prior_bars(session, code: str, limit_date: date, as_of: date) -> str | None:
    """让涨停日之前至少有三根 K 线。成功返回 None，失败返回可展示的原因。"""
    if limit_date > as_of:
        return "不能使用未来行情"
    cap = min(limit_date, as_of)
    prior = [bar for bar in load_bars(session, code, cap) if bar.trade_date < limit_date]
    if len(prior) >= 3:
        return None
    start = limit_date - timedelta(days=21)
    end = limit_date - timedelta(days=1)
    if end > as_of:
        end = as_of
    if end < start:
        return "没有可补的历史区间"
    try:
        fetched = _fetch_kline(code, start, end)
    except Exception:
        return "补拉历史日线失败"
    usable = []
    for item in fetched or []:
        day = item.get("trade_date")
        if isinstance(day, date) and day < limit_date and day <= as_of:
            usable.append(item)
    _store(session, code, usable, as_of)
    prior = [bar for bar in load_bars(session, code, cap) if bar.trade_date < limit_date]
    if len(prior) >= 3:
        return None
    return "补拉之后仍然凑不齐涨停前三根K线"
