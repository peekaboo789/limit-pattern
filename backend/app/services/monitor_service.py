"""盘中监控上一交易日的 AI 前 20 榜单。涨幅 >=3% 提醒，>=6% 报警，每只每级每天只报一次。"""

from __future__ import annotations

import threading
import time as _time
from datetime import date, datetime

from backend.app.core.clock import SHANGHAI, in_session, now
from backend.app.core.config import read_settings_file
from backend.app.core.logging import get_logger
from backend.app.models.tables import IntradayWatchAlert

logger = get_logger("monitor")

WARN_PCT = 3.0
ALERT_PCT = 6.0
OPEN_TTL = 5.0
CLOSED_TTL = 300.0
SPOT_ORDER = ("tencent", "sina", "eastmoney")

_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "key": None, "quotes": {}, "source": "", "error": ""}
_fetcher = None


def set_spot_fetcher(func=None) -> None:
    """测试用：func(codes) -> (rows, source)。"""
    global _fetcher
    _fetcher = func
    reset_cache()


def reset_cache() -> None:
    with _LOCK:
        _CACHE.update({"at": 0.0, "key": None, "quotes": {}, "source": "", "error": ""})


def level_for(pct: float | None) -> str:
    if pct is None:
        return "normal"
    if pct >= ALERT_PCT:
        return "alert"
    if pct >= WARN_PCT:
        return "warn"
    return "normal"


def market_open(moment: datetime | None = None) -> bool:
    from backend.app.core.calendar import market_open as calendar_open

    return calendar_open(moment or now())


AUCTION_OPEN = (9, 15)
AUCTION_CLOSE = (9, 25)
PHASE_TEXT = {
    "trading": "交易中",
    "auction": "集合竞价",
    "pre_open": "未开盘，报警暂停",
    "lunch": "午休，报警暂停",
    "closed": "已收盘，报警暂停",
    "non_trading_day": "非交易日，报警暂停",
}


def alert_phase(moment: datetime | None = None, settings: dict | None = None) -> dict:
    """当前处在哪个时段、是否允许报警。默认只在 9:30–11:30、13:00–15:00 的交易日报警。"""
    from datetime import time as _t

    from backend.app.core.calendar import is_trading_day

    settings = read_settings_file() if settings is None else settings
    moment = moment or now()
    local = moment.astimezone(SHANGHAI) if moment.tzinfo else moment.replace(tzinfo=SHANGHAI)
    clock = local.time()
    if not is_trading_day(local.date()):
        phase = "non_trading_day"
    elif _t(9, 30) <= clock < _t(11, 30) or _t(13, 0) <= clock < _t(15, 0):
        phase = "trading"
    elif _t(*AUCTION_OPEN) <= clock < _t(*AUCTION_CLOSE):
        phase = "auction"
    elif clock < _t(9, 30):
        phase = "pre_open"
    elif clock < _t(13, 0):
        phase = "lunch"
    else:
        phase = "closed"
    hours_only = settings.get("alert_trading_hours_only") is not False
    include_auction = settings.get("alert_include_auction") is True
    if not hours_only:
        active = True
    else:
        active = phase == "trading" or (phase == "auction" and include_auction)
    text = PHASE_TEXT[phase]
    if phase == "auction":
        text = "集合竞价" + ("" if active else "，报警暂停")
    elif not hours_only and phase != "trading":
        text = text.replace("报警暂停", "仍报警（已关闭「仅在交易时段报警」）")
    return {"phase": phase, "alerts_active": active, "text": text}


def _spot_chain() -> list[str]:
    configured = str(read_settings_file().get("data_source") or "auto").lower()
    if configured in SPOT_ORDER:
        return [configured] + [name for name in SPOT_ORDER if name != configured]
    return list(SPOT_ORDER)


def _default_fetch(codes: list[str]) -> tuple[list[dict], str]:
    from backend.app.providers.router import _module

    errors = []
    for name in _spot_chain():
        try:
            rows = _module(name).fetch_spot(codes)
        except Exception as exc:  # 换下一个源
            errors.append(f"{name}: {exc}")
            continue
        if rows:
            return rows, name
        errors.append(f"{name}: 空结果")
    raise RuntimeError("；".join(errors) or "没有可用行情源")


def _pct(row: dict) -> float | None:
    close, pre = row.get("close"), row.get("pre_close")
    if close and pre:
        return round((float(close) - float(pre)) / float(pre) * 100, 2)
    value = row.get("pct_chg")
    return None if value is None else round(float(value), 2)


def _quotes(codes: list[str], is_open: bool) -> dict:
    key = tuple(sorted(codes))
    ttl = OPEN_TTL if is_open else CLOSED_TTL
    with _LOCK:
        if _CACHE["key"] == key and _time.monotonic() - _CACHE["at"] < ttl:
            return dict(_CACHE)
    fetch = _fetcher or _default_fetch
    try:
        rows, source = fetch(list(codes))
        quotes = {str(row.get("code")): row for row in rows if row.get("code")}
        error = ""
    except Exception as exc:
        logger.warning("监控行情没有拉到：%s", exc)
        with _LOCK:
            quotes, source = dict(_CACHE["quotes"]), _CACHE["source"]
        error = str(exc)
    with _LOCK:
        _CACHE.update({"at": _time.monotonic(), "key": key, "quotes": quotes, "source": source, "error": error})
        return dict(_CACHE)


def _record(session, day: date, item: dict) -> None:
    levels = ["warn", "alert"] if item["level"] == "alert" else ["warn"]
    for tier in levels:
        exists = (
            session.query(IntradayWatchAlert.id)
            .filter_by(trade_date=day, stock_code=item["code"], tier=tier)
            .first()
        )
        if exists:
            continue
        session.add(
            IntradayWatchAlert(
                trade_date=day,
                stock_code=item["code"],
                tier=tier,
                rank=item.get("rank"),
                score=item.get("ai_score"),
                percent=item.get("pct"),
            )
        )
    session.commit()


def snapshot(session, moment: datetime | None = None) -> dict:
    from backend.app.services.ranking_service import previous_ranking

    moment = moment or now()
    day = moment.astimezone(SHANGHAI).date() if moment.tzinfo else moment.date()
    is_open = market_open(moment)
    phase = alert_phase(moment)
    active = phase["alerts_active"]
    board = previous_ranking(session, day)
    base = {
        "date": day.isoformat(),
        "market_open": is_open,
        "phase": phase["phase"],
        "alerts_active": active,
        "status_text": phase["text"],
        "ranking_date": None,
        "thresholds": {"warn": WARN_PCT, "alert": ALERT_PCT},
        "source": "",
        "error": "",
        "updated_at": moment.isoformat(timespec="seconds"),
        "items": [],
    }
    if not board or not board.get("items"):
        base["error"] = "还没有上一交易日的 AI 榜单"
        return base
    base["ranking_date"] = board.get("date")
    entries = board["items"]
    cache = _quotes([entry["code"] for entry in entries], is_open or active)
    base["source"] = cache["source"]
    base["error"] = cache["error"]
    items = []
    for entry in entries:
        quote = cache["quotes"].get(entry["code"]) or {}
        pct = _pct(quote) if quote else None
        item = {
            "rank": entry.get("rank"),
            "code": entry["code"],
            "name": entry.get("name") or quote.get("name") or "",
            "ai_score": entry.get("ai_score"),
            "reason": entry.get("reason") or "",
            "price": quote.get("close"),
            "pre_close": quote.get("pre_close"),
            "pct": pct,
            "level": level_for(pct),
        }
        items.append(item)
        if active and item["level"] != "normal":  # 非报警时段只显示最后的数字，不记报警
            _record(session, day, item)
    base["items"] = items
    base["alert_count"] = sum(1 for item in items if item["level"] == "alert") if active else 0
    base["warn_count"] = sum(1 for item in items if item["level"] == "warn")
    return base


def events(session, since: int = 0, moment: datetime | None = None) -> dict:
    moment = moment or now()
    day = moment.astimezone(SHANGHAI).date() if moment.tzinfo else moment.date()
    phase = alert_phase(moment)
    if phase["alerts_active"]:
        snapshot(session, moment)  # 非报警时段不拉行情，也不产生新报警
    rows = (
        session.query(IntradayWatchAlert)
        .filter(IntradayWatchAlert.trade_date == day, IntradayWatchAlert.id > since)
        .order_by(IntradayWatchAlert.id)
        .all()
    )
    from backend.app.models.tables import Stock

    names = {row.code: row.name for row in session.query(Stock.code, Stock.name)}
    items = [
        {
            "id": row.id,
            "code": row.stock_code,
            "name": names.get(row.stock_code, ""),
            "level": row.tier,
            "pct": row.percent,
            "rank": row.rank,
            "at": (row.created_at.isoformat(timespec="seconds") + "Z") if row.created_at else None,
        }
        for row in rows
    ]
    last = items[-1]["id"] if items else since
    if not phase["alerts_active"]:
        items = []  # 收盘后/重启后不再补发当天早些时候的报警；游标照常前移
    return {
        "date": day.isoformat(),
        "market_open": market_open(moment),
        "phase": phase["phase"],
        "alerts_active": phase["alerts_active"],
        "status_text": phase["text"],
        "last_id": last,
        "items": items,
    }
