"""数据管理页背后的四个重建任务与一份库内概况。

四个任务的共同形状：给定区间 → 挑出区间里真有行情的交易日 → 一天一天推进度。
区间留空时用「库里已有的范围」而不是今天：一个装了两周历史数据的库要能整段重算，
而不是只重算最新那一天。

概况（`status`）只报库里真有的行，缺就是 0，进度没跑过就是空——
「今天有几条样本」这一列必须来自 `limit_up_patterns`，不是现算出来的池子，
否则页面显示的数与用户点下按钮之后落库的数会是两套。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.config import retention_days
from backend.app.core.config import compare_window_days
from backend.app.core.constants import MATCH_TOP_N
from backend.app.core.database import session_scope
from backend.app.models.tables import (
    CalcBar,
    DailyFeature,
    DailyQuote,
    LimitUpPattern,
    MatchSummary,
    PatternMatch,
    SkippedRecord,
    StockTrainingRow,
)
from backend.app.services import data_job, match_service, pattern_service, skip_labels, training_service
from backend.app.services.feature_service import new_stats, public_stats, rebuild_features
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.quality_service import counts as quality_counts
from backend.app.services.retention import quote_dates

#: 各任务留空区间时的默认覆盖面。样本与扫描按「最近几天」，特征与训练按「整段库」。
DEFAULT_DAY_SCOPE = {
    "features": "all",
    "training": "all",
    "patterns": "pool",
    "scan": "latest",
}

#: 任务种类 → 跳过账里的阶段名。结果卡上的跳过原因按这个阶段翻成中文。
JOB_STAGE = {"features": "feature", "patterns": "pattern", "scan": "match", "training": "training"}

#: 跨天累加的那几列：它们数的都是「写进了多少行」，一天一天加起来才是这一轮的总量。
ADDITIVE_KEYS = (
    "feature_count",
    "pattern_count",
    "match_count",
    "summary_count",
    "target_count",
    "training_count",
    "training_blocked",
    "limit_up_count",
    "skipped_total",
)

#: 保持最后一轮取值的快照列：`pool_size` 是那一轮的样本池大小，加起来会变成「三天共 180 条样本」
#: 这种没人认得的数。`stock_count`/`universe_count` 同理——它们是「一天里多少只」，
#: 加出来是股票·日，页面上没有这一列，所以宁可留单日值。
SNAPSHOT_KEYS = ("job", "start", "end", "pool_size", "universe_count", "stock_count", "warmup_from")


def range_days(session: Session, start: date | None, end: date | None, *, kind: str) -> list[date]:
    """挑出要处理的交易日。空库直接报错，不起线程。"""
    days = quote_dates(session)
    if not days:
        raise LookupError("库里还没有行情，先执行同一步同步")
    if start is None and end is None:
        scope = DEFAULT_DAY_SCOPE[kind]
        if scope == "latest":
            return [days[-1]]
        if scope == "pool":
            return days[-compare_window_days():]
        return list(days)
    low = start or days[0]
    high = end or days[-1]
    if low > high:
        raise ValueError(f"起始日 {low} 晚于结束日 {high}")
    picked = [day for day in days if low <= day <= high]
    if not picked:
        raise LookupError(f"{low}~{high} 区间内没有已同步的交易日")
    return picked


def _merge(target: dict, incoming: dict) -> dict:
    """把一个交易日的记账并进这一轮的总账。

    加不加、覆盖不覆盖由上面那两份键名表说了算，不靠 `if key in (...)` 那种
    写一处漏一处的判断：新增一列记账时必须在这里登记，漏登记的表现是页面上那个数偏小。
    """
    for key, value in incoming.items():
        if key == "skipped":
            bucket = target.setdefault("skipped", {})
            for reason, count in dict(value).items():
                bucket[reason] = int(bucket.get(reason, 0)) + int(count)
        elif key == "date_range":
            got = set(target.get("date_range") or ())
            got.update(value if isinstance(value, (list, set, tuple)) else [str(value)])
            target["date_range"] = sorted(got)
        elif key in ADDITIVE_KEYS:
            target[key] = int(target.get(key, 0)) + int(value or 0)
        elif key in SNAPSHOT_KEYS or key not in target:
            target[key] = value
    return target


def start_features(start: date | None, end: date | None, codes: list[str] | None = None) -> dict:
    days = _days_for("features", start, end)
    low, high = days[0], days[-1]
    cache: dict = {}

    def work(session: Session, day: date, acc: dict) -> None:
        cache.setdefault("bars", load_bars_bulk(session, high))
        stats = new_stats("rebuild_features", day, day)
        rebuild_features(session, day, day, codes=codes, stats=stats, bars_by_code=cache["bars"])
        _merge(acc, stats)
        acc["start"] = low.isoformat()
        acc["end"] = high.isoformat()

    return data_job.start("features", dates=days, work=work)


def start_patterns(start: date | None, end: date | None) -> dict:
    days = _days_for("patterns", start, end)
    cache: dict = {}

    def work(session: Session, day: date, acc: dict) -> None:
        cache.setdefault("bars", load_bars_bulk(session, days[-1]))
        stats = new_stats("build_patterns", day, day)
        pattern_service.build_patterns(session, day, bars_by_code=cache["bars"], stats=stats)
        _merge(acc, stats)
        acc["start"] = days[0].isoformat()
        acc["end"] = days[-1].isoformat()

    return data_job.start("patterns", dates=days, work=work)


def start_scan(start: date | None, end: date | None) -> dict:
    days = _days_for("scan", start, end)
    cache: dict = {}

    def work(session: Session, day: date, acc: dict) -> None:
        cache.setdefault("bars", load_bars_bulk(session, days[-1]))
        stats = new_stats("scan_market", day, day)
        match_service.scan_and_store(session, day, stats=stats)
        _merge(acc, stats)
        acc["start"] = days[0].isoformat()
        acc["end"] = days[-1].isoformat()

    return data_job.start("scan", dates=days, work=work)


def start_training(start: date | None, end: date | None, codes: list[str] | None = None) -> dict:
    days = _days_for("training", start, end)
    low, high = days[0], days[-1]
    cache: dict = {}

    def work(session: Session, day: date, acc: dict) -> None:
        cache.setdefault("bars", load_bars_bulk(session, high))
        stats = new_stats("build_training", day, day)
        training_service.build_training(session, day, day, codes=codes, stats=stats, bars_by_code=cache["bars"])
        _merge(acc, stats)
        acc["start"] = low.isoformat()
        acc["end"] = high.isoformat()

    return data_job.start("training", dates=days, work=work)


def start_window_refresh() -> dict | None:
    """对比窗口改了之后，把最新一天落库的样本池与扫描按新窗口重做一遍。

    库里已有的扫描结果各自带着当时的 `pool_dates`，旧日子不动；只有最新一天
    是页面默认打开的那一天，它要是还挂着旧窗口，用户刚改完设置看到的仍是旧池子。
    从没建过样本、也没扫过的库不起任务——那里没有旧东西要换。
    槽位被占（`JobBusy`）就不抢：设置照样生效，等那个任务跑完用户再点一次扫描。
    """
    with session_scope() as session:
        days = quote_dates(session)
        if not days:
            return None
        latest = days[-1]
        has_scan = session.scalar(
            select(func.count(MatchSummary.id)).where(MatchSummary.target_date == latest)
        )
        has_patterns = session.scalar(select(func.count(LimitUpPattern.id)))
        if not has_scan and not has_patterns:
            return None
        pool = days[-compare_window_days():]
    cache: dict = {}

    def work(session: Session, day: date, acc: dict) -> None:
        cache.setdefault("bars", load_bars_bulk(session, latest))
        stats = new_stats("build_patterns", day, day)
        pattern_service.build_patterns(session, day, bars_by_code=cache["bars"], stats=stats)
        _merge(acc, stats)
        if day == latest:
            scan = new_stats("scan_market", day, day)
            match_service.scan_and_store(session, day, stats=scan)
            _merge(acc, scan)
        acc["job"] = "window_refresh"
        acc["start"] = pool[0].isoformat()
        acc["end"] = latest.isoformat()

    try:
        return data_job.start("window", dates=pool, work=work)
    except data_job.JobBusy:
        return None


def job_result(session: Session) -> dict:
    """页面上那张结果卡：任务记账 + 跑完之后库里真落了多少行。

    落库计数在任务结束后再读一次库，是因为 `stock_count` 那一列是单日快照，
    区间任务里它代表不出总量；库里数出来的行数才是「这一轮之后有多少」。
    """
    state = _job_state()
    return {
        "running": bool(state.get("running")),
        "kind": state.get("kind", ""),
        "label": state.get("label", ""),
        "labels": state.get("labels", {}),
        "progress": int(state.get("progress") or 0),
        "step": state.get("step", ""),
        "message": state.get("message", ""),
        "error": state.get("error", ""),
        "started_at": state.get("started_at", ""),
        "finished_at": state.get("finished_at", ""),
        "result": state.get("result"),
        "skip_labels": state["skip_labels"],
        "counts": _counts(session),
    }


def _job_state() -> dict:
    """任务状态，顺手把记账里那个 set 折成列表。

    `new_stats` 用 set 攒覆盖到的日期，直接进 JSON 响应会带出一套没人预期的编码；
    页面要的是一份能数出来的日期列表。
    """
    state = data_job.state()
    raw = state.get("result")
    state["result"] = public_stats(raw) if isinstance(raw, dict) else raw
    # 跳过原因的中文跟着任务状态一起走：`/data/status` 和 `/data/job` 都从这里拿，
    # 页面先读到哪一份都不会露出机器码。
    result = state["result"] if isinstance(state["result"], dict) else {}
    stage = JOB_STAGE.get(str(state.get("kind") or ""))
    state["skip_labels"] = {
        str(reason): skip_labels.label(str(reason), stage) for reason in (result.get("skipped") or {})
    }
    return state


def _days_for(kind: str, start: date | None, end: date | None) -> list[date]:
    """起线程之前先在校验用的那一眼里把日期定下来。

    `data_job` 的 `work` 跑在后台线程里，那里报错只能变成页上一句「失败」；
    「区间里没有已同步的交易日」这种话必须在请求还活着的时候说给用户。
    """
    with session_scope() as session:
        return range_days(session, start, end, kind=kind)


def status(session: Session) -> dict:
    """数据管理页那张概况表：库里真有多少行、覆盖哪几天、闸门跑没跑。"""
    days = quote_dates(session)
    pool = pattern_service.pool_pattern_dates(session, days[-1]) if days else []
    return {
        "earliest_trade_date": days[0].isoformat() if days else "",
        "latest_trade_date": days[-1].isoformat() if days else "",
        "trade_day_count": len(days),
        "retention_trading_days": retention_days(),
        "compare_window_days": compare_window_days(),
        "match_top_n": MATCH_TOP_N,
        "counts": _counts(session),
        "pool_dates": [day.isoformat() for day in pool],
        "patterns_by_date": _group_count(session, LimitUpPattern.limit_up_date, LimitUpPattern.id),
        "features_by_date": _group_count(session, DailyFeature.trade_date, DailyFeature.id),
        "matches_by_date": _group_count(session, MatchSummary.target_date, MatchSummary.id),
        "training_by_date": _group_count(session, StockTrainingRow.label_date, StockTrainingRow.id),
        "quality": quality_counts(session),
        "training": training_service.training_counts(session),
        "skips": _skip_totals(session),
        "running_job": data_job.current_job(),
        "job": _job_state(),
    }


def _counts(session: Session) -> dict:
    return {
        "daily_quotes": _count(session, DailyQuote.id),
        "calc_bars": _count(session, CalcBar.id),
        "daily_features": _count(session, DailyFeature.id),
        "limit_up_patterns": _count(session, LimitUpPattern.id),
        "pattern_matches": _count(session, PatternMatch.id),
        "match_summaries": _count(session, MatchSummary.id),
        "stock_training_rows": _count(session, StockTrainingRow.id),
    }


def _skip_totals(session: Session) -> dict:
    """跳过账按阶段的行数。数据管理页用它回答「为什么少了这些」。"""
    rows = (
        session.query(SkippedRecord.stage, func.count(SkippedRecord.id))
        .group_by(SkippedRecord.stage)
        .all()
    )
    totals = {str(stage or ""): int(count) for stage, count in rows}
    return {"total": sum(totals.values()), "by_stage": totals}


def _group_count(session: Session, date_column, id_column) -> dict[str, int]:
    """按天数行数。库里样本/特征/汇总/训练四条覆盖曲线共用这一条查询，不各写一份。"""
    rows = session.query(date_column, func.count(id_column)).group_by(date_column).all()
    return dict(
        sorted(
            (day.isoformat(), int(count))
            for day, count in rows
            if day is not None
        )
    )


def _count(session: Session, column) -> int:
    return int(session.scalar(select(func.count(column))) or 0)


__all__ = [
    "ADDITIVE_KEYS",
    "DEFAULT_DAY_SCOPE",
    "SNAPSHOT_KEYS",
    "job_result",
    "range_days",
    "start_features",
    "start_patterns",
    "start_scan",
    "start_training",
    "start_window_refresh",
    "status",
]
