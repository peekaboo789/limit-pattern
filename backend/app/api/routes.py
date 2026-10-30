"""契约里的全部接口。成功直接返回 JSON，错误用 detail。"""

from __future__ import annotations

from datetime import date as date_type
import json
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend.app import __version__
from backend.app.core.clock import today
from backend.app.core.config import exports_dir, public_settings, update_settings
from backend.app.services.export_service import content_disposition, render_export, save_copy
from backend.app.core.database import session_scope
from backend.app.models.tables import CalcBar, DailyQuote, Stock
from backend.app.providers.markets import classify, is_in_scope, normalize_code, reject_reason
from backend.app.services.retention import quote_dates
from backend.app.services.similarity_service import pool_dates, scan_market, similar_to
from backend.app.services.summary_service import generate_summary, get_summary, list_summaries
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
    reason = reject_reason(code)
    if reason == "科创板不在纳入范围":
        raise HTTPException(status_code=422, detail=reason)
    exchange, _board = classify(code)
    if exchange not in {"SSE", "SZSE"}:
        raise HTTPException(status_code=422, detail="只支持沪深 A 股")
    if reason:
        raise HTTPException(status_code=422, detail=reason)
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
        "amount_estimated": bool(row.amount_estimated),
        "turnover_rate": row.turnover_rate,
        "consecutive_boards": streak,
        "first_limit_time": None,
        "limit_type": limit_type,
    }


def _streaks(session, day: date) -> dict[str, int]:
    from backend.app.services.summary_service import streak_map

    return {code: n for (code, d), n in streak_map(session).items() if d == day}


@router.get("/health")
def health() -> dict:
    from backend.app.core.config import migrate_settings

    migrate_settings()
    with session_scope() as session:
        days = quote_dates(session)
    settings = public_settings()
    return {
        "status": "ok",
        "version": __version__,
        "data_date": days[-1].isoformat() if days else None,
        "data_source": settings["data_source"],
        "stored_days": len(days),
        "sample_pool_days": settings.get("sample_pool_trading_days"),
        "retention_days": settings.get("retention_trading_days"),
        "earliest_date": days[0].isoformat() if days else None,
        "active_source": settings.get("active_source") or "",
        "exports_dir": str(exports_dir()),
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


@router.post("/sync/full")
def post_sync_full(body: dict | None = None) -> dict:
    """补全最近 N 个交易日（5–250，默认 30）：即使今天已有数据也重新下载，并补写缺失日期的统计版总结。
    N + 8 超过当前保留天数时自动抬高保留天数并在返回里说明。"""
    from backend.app.core.config import public_settings, update_settings
    from backend.app.services.sync_service import start_sync, status as sync_status

    raw = (body or {}).get("days", 30)
    try:
        days = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="天数要是 5 到 250 的整数") from exc
    if days < 5 or days > 250:
        raise HTTPException(status_code=422, detail="天数要在 5 到 250 个交易日之间")
    before = int(public_settings()["retention_trading_days"])
    note = ""
    if days + 8 > before:
        update_settings({"retention_trading_days": min(250, days + 8)})
        after = int(public_settings()["retention_trading_days"])
        note = f"保留天数已从 {before} 自动调到 {after} 个交易日，否则补下来的数据会被清理掉"
    already = bool(sync_status().get("running"))
    task_id = start_sync(full=True, days=days)
    return {"task_id": task_id, "already_running": already, "full": True, "days": days, "note": note,
            "retention_trading_days": public_settings()["retention_trading_days"]}


@router.post("/sync")
def post_sync() -> dict:
    already = bool(status().get("running"))
    task_id = start_sync()
    return {"task_id": task_id, "already_running": already}


@router.post("/sync/cancel")
def post_sync_cancel() -> dict:
    from backend.app.services.sync_service import cancel_sync

    return cancel_sync()


@router.post("/sources/test")
def post_sources_test(body: dict | None = None) -> dict:
    from backend.app.providers.router import SOURCE_ORDER, test_sources

    names = (body or {}).get("sources") or list(SOURCE_ORDER)
    names = [name for name in names if name in SOURCE_ORDER]
    timeout = float((body or {}).get("timeout") or 8.0)
    timeout = max(2.0, min(timeout, 20.0))
    return {"timeout": timeout, "results": test_sources(timeout, names)}


@router.get("/sync/status")
def get_sync_status() -> dict:
    return status()


@router.get("/export")
def export_file(
    type: str = Query(..., alias="type"),
    date: str | None = None,
    code: str | None = None,
    top: int = Query(50, ge=1, le=500),
):
    kind = (type or "").strip().lower()
    if kind in {"ai_input", "ai_input_md"}:
        return get_ai_input(date=date, top=min(top if top != 50 else 100, 200), format="md" if kind == "ai_input_md" else "txt")
    if kind not in {"scan", "similarity", "pool", "summary", "ai_ranking", "next_day_pool", "backtest", "cluster"}:
        raise HTTPException(status_code=422, detail="导出类型只能是 scan、similarity、pool、summary、ai_ranking、next_day_pool、backtest、cluster")
    day = _parse_day(date, default_latest=True)
    normalized = _code(code) if code else None
    if kind == "similarity" and not normalized:
        raise HTTPException(status_code=422, detail="导出相似度需要股票代码")
    with session_scope() as session:
        if kind not in {"summary", "ai_ranking", "backtest", "cluster"} and day not in quote_dates(session):
            raise HTTPException(status_code=404, detail="这一天没有行情")
        try:
            text, filename = render_export(session, kind, day, normalized, top)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    body = text.encode("utf-8-sig")
    try:
        save_copy(filename, body)
    except OSError:
        pass
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": content_disposition(filename)},
    )


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
        items = [
            _limit_item(row, names, streaks.get(row.stock_code, 1))
            for row in rows
            if is_in_scope(row.stock_code)
        ]
    return {"date": day.isoformat(), "items": items}


@router.get("/pool")
def pool(date: str | None = None) -> dict:
    """样本池涨停：一次按日期区间 + is_limit_up 的索引查询，连板数一次算好。
    date 给定时只返回那一天；默认返回整池，counts 为每日家数。"""
    from backend.app.core.calendar import is_trading_day
    from backend.app.services.summary_service import streak_map

    with session_scope() as session:
        days = quote_dates(session)
        if not days:
            return {"dates": [], "items": [], "counts": {}, "latest": None, "empty_reason": "还没有行情数据，请先在总览同步"}
        as_of = days[-1]
        dates = pool_dates(session, as_of)
        names = {row.code: row.name for row in session.query(Stock.code, Stock.name)}
        streaks = streak_map(session)
        rows = (
            session.query(DailyQuote)
            .filter(DailyQuote.trade_date >= dates[0], DailyQuote.trade_date <= dates[-1], DailyQuote.is_limit_up.is_(True))
            .order_by(DailyQuote.trade_date, DailyQuote.stock_code)
            .all()
        )
        items = []
        counts = {d.isoformat(): 0 for d in dates}
        for row in rows:
            if not is_in_scope(row.stock_code):
                continue
            key = row.trade_date.isoformat()
            counts[key] = counts.get(key, 0) + 1
            if date and key != date:
                continue
            item = _limit_item(row, names, streaks.get((row.stock_code, row.trade_date), 1))
            item["limit_date"] = key
            items.append(item)
    reason = ""
    if date and not items:
        try:
            asked = date_type.fromisoformat(date)
            if not is_trading_day(asked):
                reason = f"{date} 不是交易日（周末或节假日休市）"
            elif date not in counts:
                reason = f"{date} 没有行情数据，可能还没同步或已超出样本池"
            else:
                reason = f"{date} 没有涨停股"
        except ValueError:
            reason = "日期格式不对"
    elif not items:
        reason = "样本池里一只涨停都没有：可能行情源没带昨收价、涨停没识别出来，请到总览点「补全历史」重算"
    return {
        "dates": [d.isoformat() for d in dates],
        "items": items,
        "counts": counts,
        "latest": as_of.isoformat(),
        "empty_reason": reason,
    }


def _pct_chg(close: float | None, pre_close: float | None) -> float | None:
    if close is None or not pre_close:
        return None
    return round((close - pre_close) / pre_close * 100, 4)


def _kline_bar(day: date, row, pct: float | None, estimated: bool = False) -> dict:
    return {
        "date": day.isoformat(),
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "volume": row.volume,
        "amount": row.amount,
        "amount_estimated": estimated,
        "pct_chg": pct,
    }


@router.get("/kline")
def kline(code: str, days: int = Query(30, ge=1, le=120)) -> dict:
    normalized = _code(code)
    with session_scope() as session:
        stock = session.get(Stock, normalized)
        merged: dict[date, dict] = {}
        calc_rows = (
            session.query(CalcBar)
            .filter(CalcBar.stock_code == normalized, CalcBar.trade_date <= today())
            .all()
        )
        for row in calc_rows:
            merged[row.trade_date] = _kline_bar(row.trade_date, row, _pct_chg(row.close, row.pre_close), False)
        rows = (
            session.query(DailyQuote)
            .filter(DailyQuote.stock_code == normalized, DailyQuote.trade_date <= today())
            .all()
        )
        for row in rows:
            merged[row.trade_date] = _kline_bar(
                row.trade_date,
                row,
                row.change_percent,
                bool(getattr(row, "amount_estimated", False)),
            )
        ordered = [merged[day] for day in sorted(merged)]
        bars = ordered[-days:]
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
        return generate_summary(session, parsed)


@router.post("/ai-ranking/generate")
def post_ai_ranking(body: dict | None = None) -> dict:
    from backend.app.services.ranking_service import generate_ranking
    from backend.app.services.summary_service import AiFailed, AiNotConfigured, ai_ready

    raw_day = (body or {}).get("date")
    day = _parse_day(raw_day, default_latest=False) if raw_day else None
    prefer = "morning" if (body or {}).get("snapshot") == "morning" else "auto"
    with session_scope() as session:
        try:
            return generate_ranking(session, day, trigger="manual", prefer=prefer)
        except AiNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AiFailed as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ai-ranking")
def get_ai_ranking(date: str | None = None) -> dict:
    from backend.app.services.ranking_service import get_ranking

    day = _parse_day(date, default_latest=False) if date else None
    with session_scope() as session:
        payload = get_ranking(session, day)
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有 AI 榜单")
    return payload


@router.get("/ai-ranking/dates")
def get_ai_ranking_dates() -> dict:
    from backend.app.services.ranking_service import ranking_dates

    with session_scope() as session:
        return {"dates": ranking_dates(session)}


@router.get("/monitor")
def get_monitor() -> dict:
    from backend.app.services.monitor_service import snapshot

    with session_scope() as session:
        return snapshot(session)


@router.get("/monitor/events")
def get_monitor_events(since: int = Query(0, ge=0)) -> dict:
    from backend.app.services.monitor_service import events

    with session_scope() as session:
        return events(session, since)


@router.get("/calendar")
def get_calendar(year: int | None = None) -> dict:
    from backend.app.core.calendar import calendar_info, is_trading_day, market_open, next_trading_day, previous_trading_day

    day = today()
    info = calendar_info(year or day.year)
    info.update(
        {
            "today": day.isoformat(),
            "today_is_trading_day": is_trading_day(day),
            "market_open": market_open(),
            "previous_trading_day": previous_trading_day(day).isoformat(),
            "next_trading_day": next_trading_day(day).isoformat(),
        }
    )
    return info


@router.get("/snapshot/status")
def get_snapshot_status() -> dict:
    from backend.app.core.clock import now
    from backend.app.services.intraday_service import load_morning_freeze, snapshot_kind

    moment = now()
    frozen = load_morning_freeze(moment.date())
    return {
        "kind_now": snapshot_kind(moment),
        "morning_freeze": None if not frozen else {"taken_at": frozen["taken_at"], "count": len(frozen["rows"])},
    }


@router.get("/backtest")
def get_backtest(kind: str = "similarity", top: int = Query(50, ge=5, le=200)) -> dict:
    from backend.app.services.backtest_service import ranking_backtest, similarity_backtest

    with session_scope() as session:
        if kind == "ai_ranking":
            return ranking_backtest(session)
        if kind == "similarity":
            return similarity_backtest(session, top)
    raise HTTPException(status_code=422, detail="kind 只能是 similarity 或 ai_ranking")


@router.get("/next-day-pool")
def get_next_day_pool(date: str | None = None, top: int = Query(30, ge=5, le=100)) -> dict:
    from backend.app.services.backtest_service import next_day_pool

    day = _parse_day(date, default_latest=False) if date else None
    with session_scope() as session:
        return next_day_pool(session, day, top)


@router.get("/cluster/status")
def get_cluster_status() -> dict:
    from backend.app.algorithms.clustering import MISSING_MESSAGE, sklearn_available

    ok = sklearn_available()
    return {"available": ok, "message": "" if ok else MISSING_MESSAGE}


@router.post("/cluster/run")
def post_cluster_run(body: dict | None = None) -> dict:
    from backend.app.services.cluster_service import ClusterUnavailable, run_cluster

    body = body or {}
    day = _parse_day(body.get("date"), default_latest=True)
    raw_k = body.get("k", "auto")
    try:
        k = None if raw_k in (None, "", "auto") else int(raw_k)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="k 应为 2~6 或 auto") from exc
    method = str(body.get("method") or "kmeans")
    with session_scope() as session:
        try:
            return run_cluster(session, day, k, method, interpret=bool(body.get("interpret")))
        except ClusterUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/cluster")
def get_cluster_result(date: str | None = None) -> dict:
    from backend.app.services.cluster_service import get_cluster

    day = _parse_day(date, default_latest=False) if date else None
    payload = get_cluster(day)
    if payload is None:
        raise HTTPException(status_code=404, detail="还没有聚类结果")
    return payload


@router.get("/cluster/features")
def get_cluster_features(date: str | None = None, format: str = "json"):
    import json as _json

    from backend.app.services.cluster_service import build_dataset, dataset_to_csv

    day = _parse_day(date, default_latest=True)
    with session_scope() as session:
        dataset = build_dataset(session, day)
    if format == "csv":
        body = dataset_to_csv(dataset).encode("utf-8-sig")
        name, media = f"features_{day.isoformat()}.csv", "text/csv; charset=utf-8"
    else:
        body = (_json.dumps(dataset, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        name, media = f"features_{day.isoformat()}.json", "application/json"
    return Response(content=body, media_type=media, headers={"Content-Disposition": content_disposition(name)})


@router.post("/ai/test")
def post_ai_test(body: dict | None = None) -> dict:
    """用一句很短的提示测试 AI 接口。正文可带未保存的 ai_base_url / ai_model / ai_api_key（密钥不回显）。"""
    from backend.app.core.ai_client import test_ai
    from backend.app.core.config import read_settings_file

    settings = dict(read_settings_file())
    for field in ("ai_base_url", "ai_model", "ai_api_key", "ai_thinking"):
        value = (body or {}).get(field)
        if value:
            settings[field] = str(value).strip()
    return test_ai(settings)


@router.get("/ai-input")
def get_ai_input(date: str | None = None, top: int = Query(100, ge=1, le=200), format: str = "txt"):
    """导出前 100 给外部 AI 分析：TXT 或 MD。不带日期时盘中用实时快照，否则用最新正式日线。"""
    from backend.app.services.ai_export import build_ai_input
    from backend.app.services.export_service import content_disposition, save_copy
    from backend.app.services.intraday_service import provisional_day

    fmt = "md" if str(format).lower() in {"md", "markdown"} else "txt"
    with session_scope() as session:
        try:
            if date:
                day = _parse_day(date, default_latest=True)
                if day not in quote_dates(session):
                    raise HTTPException(status_code=404, detail="这一天没有行情")
                text, filename = build_ai_input(session, day, None, top, fmt)
            else:
                with provisional_day(session) as (as_of, info):
                    if as_of is None:
                        raise HTTPException(status_code=404, detail="还没有行情")
                    text, filename = build_ai_input(session, as_of, info, top, fmt)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    body = text.encode("utf-8")
    try:
        save_copy(filename, body)
    except OSError:
        pass
    media = "text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8"
    return Response(content=body, media_type=media, headers={"Content-Disposition": content_disposition(filename)})


@router.post("/ai-ranking/import")
def post_ai_ranking_import(body: dict) -> dict:
    """导入外部 AI 返回的前 20 JSON，存为 trigger=external 的榜单，监控照常使用。"""
    from backend.app.services.ai_export import import_external
    from backend.app.services.summary_service import AiFailed

    text = (body or {}).get("text")
    if not isinstance(text, str):
        text = json.dumps((body or {}).get("json") if (body or {}).get("json") is not None else body, ensure_ascii=False)
    if not text.strip():
        raise HTTPException(status_code=422, detail="请粘贴外部 AI 返回的 JSON")
    if len(text) > 500_000:
        raise HTTPException(status_code=422, detail="内容太长")
    day = _parse_day(body["date"], default_latest=False) if (body or {}).get("date") else None
    with session_scope() as session:
        try:
            return import_external(session, text, day, str((body or {}).get("model") or ""))
        except AiFailed as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/ai-ranking/progress")
def get_ai_ranking_progress() -> dict:
    from backend.app.services.ranking_service import progress

    return progress()
