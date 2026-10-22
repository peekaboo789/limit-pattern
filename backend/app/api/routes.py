"""契约里的全部接口。成功直接返回 JSON，错误用 detail。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from backend.app import __version__
from backend.app.core.clock import today
from backend.app.core.config import public_settings, update_settings
from backend.app.core.database import session_scope
from backend.app.models.tables import DailyQuote, Stock
from backend.app.providers.markets import classify, normalize_code
from backend.app.services.retention import quote_dates
from backend.app.services.similarity_service import pool_dates, scan_market, similar_to
from backend.app.services.summary_service import (
    AiFailed,
    AiNotConfigured,
    generate_summary,
    get_summary,
    list_summaries,
)
from backend.app.services.sync_service import start_sync, status

router = APIRouter()


def _parse_day(value: str | None, *, default_latest: bool) -> date:
    if not value:
        if not default_latest:
            raise HTTPException(status_code=422, detail="缺少日期")
        with session_scope() as session:
            days = quote_dates(session)
        if not days:
            raise HTTPException(status_code=404, detail="还没有行情")
        return days[-1]
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from exc
    if day > today():
        raise HTTPException(status_code=400, detail="不能查询未来的日期")
    return day


def _code(value: str) -> str:
    code = normalize_code(value)
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=422, detail="股票代码应为 6 位数字")
    exchange, _board = classify(code)
    if exchange not in {"SSE", "SZSE"}:
        raise HTTPException(status_code=422, detail="只支持沪深 A 股")
    return code


def _limit_item(row: DailyQuote, names: dict[str, str], streak: int) -> dict:
    exchange, _board = classify(row.stock_code)
    one_word = (
        row.open is not None
        and row.high is not None
        and row.low is not None
        and row.close is not None
        and abs(row.open - row.close) < 1e-6
        and abs(row.high - row.low) < 1e-6
    )
    if one_word:
        limit_type = "一字板"
    elif row.limit_up_percent:
        limit_type = f"{int(row.limit_up_percent)}%涨停"
    else:
        limit_type = "收盘涨停"
    return {
        "code": row.stock_code,
        "name": names.get(row.stock_code, ""),
        "board": "SH" if exchange == "SSE" else "SZ",
        "close": row.close,
        "pct_chg": row.change_percent,
        "volume": row.volume,
        "amount": row.amount,
        "turnover_rate": row.turnover_rate,
        "consecutive_boards": streak,
        "first_limit_time": None,
        "limit_type": limit_type,
    }


def _streaks(session, day: date) -> dict[str, int]:
    from backend.app.services.summary_service import _streak

    rows = (
        session.query(DailyQuote.stock_code)
        .filter(DailyQuote.trade_date == day, DailyQuote.is_limit_up.is_(True))
        .all()
    )
    return {code: _streak(session, code, day) for (code,) in rows}


@router.get("/health")
def health() -> dict:
    with session_scope() as session:
        days = quote_dates(session)
    return {
        "status": "ok",
        "version": __version__,
        "data_date": days[-1].isoformat() if days else None,
    }


@router.get("/settings")
def get_settings_view() -> dict:
    return public_settings()


@router.put("/settings")
def put_settings(body: dict) -> dict:
    try:
        return update_settings(body or {})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sync")
def post_sync() -> dict:
    return {"task_id": start_sync()}


@router.get("/sync/status")
def get_sync_status() -> dict:
    return status()


@router.get("/limitups")
def limitups(date: str | None = None) -> dict:
    day = _parse_day(date, default_latest=True)
    with session_scope() as session:
        if day not in quote_dates(session):
            raise HTTPException(status_code=404, detail="这一天没有行情")
        names = {row.code: row.name for row in session.query(Stock)}
        streaks = _streaks(session, day)
        rows = (
            session.query(DailyQuote)
            .filter(DailyQuote.trade_date == day, DailyQuote.is_limit_up.is_(True))
            .order_by(DailyQuote.stock_code)
            .all()
        )
        items = [_limit_item(row, names, streaks.get(row.stock_code, 1)) for row in rows]
    return {"date": day.isoformat(), "items": items}


@router.get("/pool")
def pool() -> dict:
    with session_scope() as session:
        days = quote_dates(session)
        if not days:
            return {"dates": [], "items": []}
        as_of = days[-1]
        dates = pool_dates(session, as_of)
        names = {row.code: row.name for row in session.query(Stock)}
        items = []
        for day in dates:
            streaks = _streaks(session, day)
            rows = (
                session.query(DailyQuote)
                .filter(DailyQuote.trade_date == day, DailyQuote.is_limit_up.is_(True))
                .order_by(DailyQuote.stock_code)
                .all()
            )
            for row in rows:
                item = _limit_item(row, names, streaks.get(row.stock_code, 1))
                item["limit_date"] = day.isoformat()
                items.append(item)
    return {"dates": [day.isoformat() for day in dates], "items": items}


@router.get("/kline")
def kline(code: str, days: int = Query(30, ge=1, le=120)) -> dict:
    normalized = _code(code)
    with session_scope() as session:
        stock = session.get(Stock, normalized)
        rows = (
            session.query(DailyQuote)
            .filter(DailyQuote.stock_code == normalized, DailyQuote.trade_date <= today())
            .order_by(DailyQuote.trade_date)
            .all()
        )
        bars = [
            {
                "date": row.trade_date.isoformat(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
                "pct_chg": row.change_percent,
            }
            for row in rows[-days:]
        ]
    return {"code": normalized, "name": stock.name if stock else "", "bars": bars}


@router.get("/similarity")
def similarity(code: str, date: str | None = None, top: int = Query(20, ge=1, le=200)) -> dict:
    normalized = _code(code)
    day = _parse_day(date, default_latest=True)
    with session_scope() as session:
        if day not in quote_dates(session):
            raise HTTPException(status_code=404, detail="这一天没有行情")
        try:
            return similar_to(session, normalized, day, top)
        except LookupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/scan")
def scan(
    date: str | None = None,
    top: int = Query(50, ge=1, le=200),
    min_score: float = Query(0),
) -> dict:
    day = _parse_day(date, default_latest=True)
    with session_scope() as session:
        if day not in quote_dates(session):
            raise HTTPException(status_code=404, detail="这一天没有行情")
        return scan_market(session, day, top, min_score)


@router.get("/summaries")
def summaries() -> dict:
    with session_scope() as session:
        return list_summaries(session)


@router.get("/summaries/{day}")
def summary_detail(day: str) -> dict:
    parsed = _parse_day(day, default_latest=False)
    with session_scope() as session:
        payload = get_summary(session, parsed)
    if payload is None:
        raise HTTPException(status_code=404, detail="这一天还没有总结")
    return payload


@router.post("/summaries/{day}/generate")
def summary_generate(day: str) -> dict:
    parsed = _parse_day(day, default_latest=False)
    with session_scope() as session:
        if parsed not in quote_dates(session):
            raise HTTPException(status_code=404, detail="这一天没有行情")
        try:
            return generate_summary(session, parsed)
        except AiNotConfigured as exc:
            raise HTTPException(status_code=409, detail="未配置AI接口") from exc
        except AiFailed as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
