"""契约里的全部接口。成功直接返回 JSON，错误用 detail。"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend.app import __version__
from backend.app.core.clock import today
from backend.app.core.config import exports_dir, public_settings, update_settings
from backend.app.services.export_service import content_disposition, render_export, save_copy
from backend.app.core.database import session_scope
from backend.app.models.tables import CalcBar, DailyQuote, Stock
from backend.app.core.constants import REALTIME_TOP_N_MAX, REALTIME_TOP_N_MIN
from backend.app.providers.markets import classify, is_in_scope, normalize_code, reject_reason
from backend.app.services.intraday_watch import watcher
from backend.app.services.intraday_watch_store import TIERS
from backend.app.services.realtime_tracker import RANK_METRICS, tracker
from backend.app.services import ai_picks
from backend.app.services.ai_analysis_service import (
    board_runner,
    board_selection,
    one_row_board,
    single_runner,
    single_selection,
)
from backend.app.services.ai_analysis_store import list_reports, report_detail
from backend.app.services.ai_daily_rank_service import daily_rank_runner
from backend.app.services.ai_daily_rank_service import get_board as daily_rank_row
from backend.app.services.ai_daily_rank_service import history as daily_rank_history
from backend.app.services.ai_daily_rank_service import prepare_from_tracker
from backend.app.services.ai_daily_rank_service import state as daily_rank_state
from backend.app.services.ai_gateway import AiNotConfigured
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
    settings = public_settings()
    return {
        "status": "ok",
        "version": __version__,
        "data_date": days[-1].isoformat() if days else None,
        "data_source": settings["data_source"],
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


@router.post("/sync")
def post_sync() -> dict:
    return {"task_id": start_sync()}


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
    if kind not in {"scan", "similarity", "pool", "summary"}:
        raise HTTPException(status_code=422, detail="导出类型只能是 scan、similarity、pool、summary")
    day = _parse_day(date, default_latest=True)
    normalized = _code(code) if code else None
    if kind == "similarity" and not normalized:
        raise HTTPException(status_code=422, detail="导出相似度需要股票代码")
    with session_scope() as session:
        if kind != "summary" and day not in quote_dates(session):
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
                if not is_in_scope(row.stock_code):
                    continue
                item = _limit_item(row, names, streaks.get(row.stock_code, 1))
                item["limit_date"] = day.isoformat()
                items.append(item)
    return {"dates": [day.isoformat() for day in dates], "items": items}


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


@router.get("/realtime/top")
def realtime_top(metric: str | None = None, top_n: int | None = None) -> dict:
    """盘中监控的榜：榜前 N 只每 2 秒随请求重算，全市场每 120 秒由后台线程跑一轮。"""
    if metric is not None and metric not in RANK_METRICS:
        raise HTTPException(status_code=422, detail="没有这个排序指标")
    if top_n is not None and (top_n < REALTIME_TOP_N_MIN or top_n > REALTIME_TOP_N_MAX):
        raise HTTPException(status_code=422, detail=f"榜单条数要在 {REALTIME_TOP_N_MIN} 到 {REALTIME_TOP_N_MAX} 之间")
    return tracker.top(metric=metric, top_n=top_n)


@router.get("/realtime/status")
def realtime_status() -> dict:
    """刷新节奏、当前时段、形态窗口用的是哪根 K 线。不触发扫描。"""
    return tracker.status()


# --- 盘中涨幅报警榜（前一交易日固定榜那几名，今天盯涨幅）--------------------


@router.get("/realtime/intraday-watch", response_model=dict)
def intraday_watch_state() -> dict[str, Any]:
    """报警榜的整份状态：池子、每只今天的涨幅与近 3 分钟跳升、报过哪些、还没确认哪些。

    读的是最近一轮采样的结果，**不在请求里跑网络轮次**——采样由后台线程按 3 秒节奏跑，
    否则窗口一收进托盘就断采，报警只剩一个好看的浮层。

    `status` 五个值是五件不同的事，页面各说各话：`watching` 在采；`pre_open` / `after_close`
    时段不对，不敲上游；`no_pool` 没有可盯的池子（四种缺池各有各的话，在 `pool.note` 里）；
    `unavailable` 取不到快照，或数据源回的还不是今天的价。

    这里没有一条会花钱的路径：读的是价格与库里已有的名次表，一次网关都不敲。
    """
    return watcher.state()


@router.post("/realtime/intraday-watch/ack", response_model=dict)
def intraday_watch_ack(
    stock_code: str | None = Query(None, description="只确认这一只；不给就是全部"),
    tier: str | None = Query(None, description=" | ".join(TIERS) + "；不给就是这一只的两档"),
) -> dict[str, Any]:
    """点「知道了」：写上确认时刻，返回还剩几条没确认、窗口该不该继续置顶。

    置顶**只由这一句解除**，前端自己记一个布尔变量是不行的：刷新一次、换个浏览器、
    服务重启，那个变量就没了，而窗口还摞在最前面，用户看不出为什么。
    确认只作用在**今天**这一天的账上（去重键就是「交易日 + 代码 + 档」），
    昨天那几条没点的不会被这一句顺手清掉，也不会再来敲今天这一次。
    """
    if tier is not None and tier not in TIERS:
        raise HTTPException(
            status_code=422, detail=f"没有这个报警档位，可选 {' | '.join(TIERS)}"
        )
    day = today().isoformat()
    result = watcher.ack(day, stock_code=stock_code, tier=tier)
    return {"trade_date": day, "stock_code": stock_code, "tier": tier, **result}


# --- AI 综合分析（盘中整榜与单股直评）----------------------------------------


def _current_board() -> tuple[dict, dict]:
    """屏幕上那一份榜，连同「它是按什么口径选出来的」。

    分析用的榜必须与页面显示的是同一批打分结果：另取一次价会变成两套数，
    而页面上那句「我分析的就是这一榜」就成了假话。取不到就当场说清是预热中还是报错了。
    """
    payload = tracker.top()
    snapshot = payload.get("snapshot")
    if not snapshot:
        raise HTTPException(
            status_code=409,
            detail=f"监控榜还没有可用的一份（状态 {payload.get('status')}："
            f"{payload.get('error') or '这一轮还没跑出全市场结果'}）",
        )
    selection = board_selection(
        metric=str(payload.get("metric") or ""),
        metric_label=str(payload.get("metric_label") or ""),
        top_n=int(payload.get("top_n") or 0),
    )
    snapshot["metric"] = selection["metric"]
    snapshot["metric_label"] = selection["metric_label"]
    snapshot["top_n"] = selection["top_n"]
    return snapshot, selection


@router.post("/ai/analysis")
def ai_analysis_start() -> dict:
    """起一次整榜分析。已经有一份在跑就接着等那一份，不重复花钱。"""
    board, selection = _current_board()
    try:
        return board_runner.start("", lambda: board, selection=selection)
    except AiNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ai/analysis")
def ai_analysis_state() -> dict:
    """轮询读的那一份：在跑就给进度（已收到多少字），跑完给结论。

    内存里没有就从存档兜底——重开程序或十分钟到了，页面上不该变成「什么都没有」。

    先读进度、后读结论：这一对必须按这个顺序读。反过来读的话，轮询可能卡在
    「结论还没落地、进度已经清掉」那一瞬之间——页面显示「没在跑、也没有结论」，
    用户只能重进页面才看得见那份已经跑完的分析。
    """
    running = board_runner.status()
    with session_scope() as session:
        result = board_runner.result("")
        report = result.get("report")
        if report and report.get("ok"):
            report["pick_history"] = ai_picks.history_overview(
                session, [str(pick.get("stock_code") or "") for pick in report.get("picks") or []]
            )
        return {"runner": running, "source": result.get("source"), "report": report}


@router.post("/ai/single/{code}")
def ai_single_start(code: str) -> dict:
    """单股直评：只把这一只股票的证据交给模型，最多回一条结论。"""
    key = _code(code)
    if not is_in_scope(key):
        raise HTTPException(status_code=422, detail=f"{key} 不在统计范围内：{reject_reason(key)}")

    def provider() -> dict:
        with session_scope() as session:
            return one_row_board(session, key)

    try:
        return single_runner.start(key, provider, selection=single_selection(key))
    except AiNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ai/single/{code}")
def ai_single_state(code: str) -> dict:
    """这一只股票的直评结论 + 它被推荐过的老账（从没推过就是 null，不是 0 次）。

    读取顺序和整榜那一份一样：先问在不在跑，再取结论。
    """
    key = _code(code)
    running = single_runner.status(key)
    with session_scope() as session:
        result = single_runner.result(key)
        report = result.get("report")
        if report and report.get("picks"):
            report["ai_history"] = ai_picks.pick_history(session, key)
        return {
            "code": key,
            "runner": running,
            "source": result.get("source"),
            "report": report,
            "ai_history": ai_picks.pick_history(session, key),
        }


@router.get("/ai/reports")
def ai_reports(chain: str | None = None, limit: int = Query(20, ge=1, le=100)) -> dict:
    with session_scope() as session:
        return list_reports(session, chain=chain or "", limit=limit)


@router.get("/ai/reports/{report_id}")
def ai_report_detail(report_id: int) -> dict:
    with session_scope() as session:
        payload = report_detail(session, report_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="没有这份存档")
    return payload


# --- AI 推荐的对账账本（零成本）----------------------------------------------


@router.get("/ai/picks/summary")
def ai_picks_summary() -> dict:
    with session_scope() as session:
        return ai_picks.summary(session)


@router.post("/ai/picks/check")
def ai_picks_check() -> dict:
    """把待验证的行和库里已有的行情对一遍。只读库，一次网关都不敲。"""
    with session_scope() as session:
        check = ai_picks.reconcile(session)
        summary = ai_picks.summary(session)
    return {"reconcile": check, **summary}


@router.get("/ai/picks")
def ai_picks_list(
    date: str | None = None, verdict: str | None = None, limit: int = Query(200, ge=1, le=500)
) -> dict:
    with session_scope() as session:
        return ai_picks.result_rows(session, trade_date=date or "", verdict=verdict or "", limit=limit)


@router.get("/ai/picks/{code}")
def ai_picks_history(code: str) -> dict:
    key = _code(code)
    with session_scope() as session:
        history = ai_picks.pick_history(session, key)
    #: 从没被推荐过是第一等答案，不是一片 0 的空表
    return {"code": key, "never_recommended": history is None, "history": history}


# --- 每日 AI 固定榜 ----------------------------------------------------------


@router.get("/ai/daily-rank")
def ai_daily_rank(date: str | None = None) -> dict:
    """某一天那份固定榜：备好的输入、排出的名次、校验闸的计数、有没有在跑。"""
    day = "" if not date else str(_parse_day(date, default_latest=False))
    with session_scope() as session:
        return daily_rank_state(session, day)


@router.post("/ai/daily-rank/prepare")
def ai_daily_rank_prepare() -> dict:
    """备好那 100 行输入。这一步是本系统自己算的，一分钱不花、一次网关都不敲。"""
    with session_scope() as session:
        try:
            return prepare_from_tracker(session)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/ai/daily-rank/rank")
def ai_daily_rank_start(date: str | None = None) -> dict:
    """排出前 20 名：这是这一页唯一会花钱的按钮。"""
    day = str(_parse_day(date, default_latest=True)) if date else str(today())
    with session_scope() as session:
        row = daily_rank_row(session, day)
        feed = ((row.payload or {}).get("prepared") or {}).get("feed") if row else None
    if row is None or not feed:
        raise HTTPException(
            status_code=409,
            detail=f"{day} 没有备好的输入（这一天没备过，或那天不是交易日）。先点「备好输入」，那一步不花钱",
        )
    try:
        return daily_rank_runner.start(day)
    except AiNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ai/daily-rank/status")
def ai_daily_rank_status() -> dict:
    """只问「在不在跑」，不碰库——页面上那 2 秒一拍读的就是这个。"""
    return daily_rank_runner.status()


@router.get("/ai/daily-rank/history")
def ai_daily_rank_history(limit: int = Query(15, ge=1, le=60)) -> dict:
    with session_scope() as session:
        return daily_rank_history(session, limit=limit)


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
