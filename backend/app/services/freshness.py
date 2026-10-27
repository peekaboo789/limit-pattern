"""库里「该有的一天」到底有没有落到位——这一件事只在这里判一次。

`sync_service` 只写 `daily_quotes`。特征、涨停样本、匹配扫描这三层是从行情**派生**出来的，
过去只有数据管理页那四个按钮会写它们。于是两种很常见的情形会把页面钉在旧数据上：

* 收盘后没赶上 17:10 那班车（软件没开、电脑睡了），行情本身就少一天；
* 行情有这一天，可没人点过「重建涨停样本」，样本池里就是没有它——
  用户看到的是「26 号的涨停数据还显示缺失」，而它其实一行行情都没少，少的只是派生层。

所以这里给两个入口同一把尺子：

* `sync_service` 的收尾（`derive_pass`）：行情刚落库，顺手把三层补到最新那个**已收盘**的日子；
* 服务启动（`main.bootstrap_data` → `start_derive`）：上一进程没赶上班车，开机第一次打开也得自己长回来。

判据一律读库里的真行数，不另立「跑过没跑过」的记号——记号会和实际内容脱节，
而「有涨停却没有样本」这种形状本身就是缺数据的证据，补一次就消失，不会天天重跑。
唯一可能反复触发的是「某天涨停的那几只全都因凑不齐三天被跳过」，那种日子样本数永远是 0，
每轮重算的代价是一天几行，换的是「补拉历史晚到一步也能自己长回来」。

训练集（`stock_training_rows`）**不在**自动补齐之列：那是评分模型的输入，
悄悄换掉它等于悄悄换掉模型，页面上它有自己的按钮。
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core import clock
from backend.app.core.database import session_scope
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyFeature, DailyQuote, LimitUpPattern, MatchSummary
from backend.app.providers.markets import is_in_scope
from backend.app.services import data_job, match_service, pattern_service
from backend.app.services.feature_service import new_stats, rebuild_features
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.pattern_service import pool_pattern_dates
from backend.app.services.quality_service import is_open_day
from backend.app.services.retention import quote_dates

logger = get_logger("freshness")


def expected_quote_day(moment: datetime | None = None) -> date:
    """最近一个「本该已经有完整日线」的日子。

    上限是 `clock.settled_day()`（当天那根线要 15:00 之后才算落定），再往前跳过周末。
    法定节假日判不出来——这一版没有交易日历源，跟 `quality_service.is_open_day` 同一套取舍：
    顶多多跑一次同步，不会少跑。
    """
    day = clock.settled_day(moment)
    while not is_open_day(day):
        day -= timedelta(days=1)
    return day


def _distinct(session: Session, column) -> set[date]:
    return {row[0] for row in session.query(column).distinct() if row[0] is not None}


def _limit_up_days(session: Session) -> set[date]:
    """有「在口径内的涨停」的那些天。科创板这类被排除的板块不算缺样本。"""
    rows = session.query(DailyQuote.trade_date, DailyQuote.stock_code).filter(
        DailyQuote.is_limit_up.is_(True)
    )
    return {day for day, code in rows if is_in_scope(code)}


def gaps(session: Session, moment: datetime | None = None) -> dict:
    """缺哪几层、该补哪几天。不写库，纯读。

    `settled_day` 是这次判断的上限：盘中那半天不算一天，谁也不许拿它去建样本。
    """
    settled = clock.settled_day(moment)
    days = quote_dates(session)
    settled_days = [day for day in days if day <= settled]
    verdict = {
        "settled_day": settled,
        "expected_quote_day": expected_quote_day(moment),
        "latest_quote_day": days[-1] if days else None,
        "empty_db": not days,
        "quotes_behind": not days or days[-1] < expected_quote_day(moment),
        "features": [],
        "patterns": [],
        "scan": None,
    }
    if not settled_days:
        return verdict
    feature_days = _distinct(session, DailyFeature.trade_date)
    pattern_days = _distinct(session, LimitUpPattern.limit_up_date)
    summary_days = _distinct(session, MatchSummary.target_date)
    limit_days = _limit_up_days(session)
    latest = settled_days[-1]
    verdict["features"] = [day for day in settled_days if day not in feature_days]
    pool = [day for day in pool_pattern_dates(session, latest) if day <= settled]
    verdict["patterns"] = [
        day for day in pool if day in limit_days and day not in pattern_days
    ]
    if latest not in summary_days:
        verdict["scan"] = latest
    return verdict


def needs_work(verdict: dict) -> bool:
    return bool(verdict["features"] or verdict["patterns"] or verdict["scan"])


def _days_to_build(verdict: dict) -> list[date]:
    """要动的日子合成一条升序队列：扫描排在最后，它要用的是前面刚补好的样本池。"""
    picked = set(verdict["features"]) | set(verdict["patterns"])
    if verdict["scan"]:
        picked.add(verdict["scan"])
    return sorted(picked)


def _summarise(acc: dict, days: list[date]) -> str:
    """给同步消息与日志用的一句人话。"""
    span = f"{days[0].isoformat()}~{days[-1].isoformat()}" if len(days) > 1 else days[0].isoformat()
    bits = []
    if acc.get("feature_count"):
        bits.append(f"特征 {acc['feature_count']} 行")
    if acc.get("pattern_count"):
        bits.append(f"样本 {acc['pattern_count']} 条")
    if acc.get("summary_count"):
        bits.append(f"扫描汇总 {acc['summary_count']} 条")
    if not bits:
        return f"，自动补齐扫过 {span}，这几层本来就在"
    return f"，已自动补齐 {span} 的" + "、".join(bits)


def derive_pass(moment: datetime | None = None) -> dict:
    """把缺的派生层补上。**在当前线程跑完**，占 `data_job` 那个槽位。

    回 `{"ran": bool, "note": str, "days": [...]}`：note 是一句人话，
    同步收尾把它拼进自己的完成消息里，页面上看得见这一轮到底补了什么。
    """
    with session_scope() as session:
        verdict = gaps(session, moment)
    days = _days_to_build(verdict)
    if not days:
        return {"ran": False, "note": "，派生数据已是最新", "days": []}
    want_features = set(verdict["features"])
    want_patterns = set(verdict["patterns"])
    scan_day = verdict["scan"]
    cache: dict = {}

    def work(session: Session, day: date, acc: dict) -> None:
        cache.setdefault("bars", load_bars_bulk(session, days[-1]))
        if day in want_features:
            stats = new_stats("rebuild_features", day, day)
            rebuild_features(session, day, day, stats=stats, bars_by_code=cache["bars"])
            acc["feature_count"] = int(acc.get("feature_count") or 0) + int(
                stats.get("feature_count") or 0
            )
        if day in want_patterns:
            stats = new_stats("build_patterns", day, day)
            pattern_service.build_patterns(session, day, bars_by_code=cache["bars"], stats=stats)
            acc["pattern_count"] = int(acc.get("pattern_count") or 0) + int(
                stats.get("pattern_count") or 0
            )
        if day == scan_day:
            stats = new_stats("scan_market", day, day)
            match_service.scan_and_store(session, day, stats=stats)
            acc["summary_count"] = int(acc.get("summary_count") or 0) + int(
                stats.get("summary_count") or 0
            )
        acc["days_done"] = acc.get("days_done", []) + [day.isoformat()]

    try:
        receipt = data_job.run("derive", dates=days, work=work)
    except data_job.JobBusy as exc:
        note = f"，自动补齐让路：{exc}"
        logger.info("自动补齐没有跑：%s", exc)
        return {"ran": False, "note": note, "days": days}
    state = data_job.state()
    if state.get("task_id") != receipt.get("task_id"):
        # 槽位已经交给下一个任务，这一轮的记账拿不到了。宁可只报「补过哪几天」，
        # 也不能把别人那一轮的行数当成自己的写进消息里。
        span = f"{days[0].isoformat()}~{days[-1].isoformat()}" if len(days) > 1 else days[0].isoformat()
        return {"ran": True, "note": f"，已自动补齐 {span}（这一轮的记账被后来的任务顶掉了）", "days": days}
    if state.get("error"):
        return {"ran": True, "note": f"，自动补齐没有跑成：{state['error']}", "days": days}
    acc = state.get("result") or {}
    return {"ran": True, "note": _summarise(acc, days), "days": days, "counts": acc}


def start_derive(moment: datetime | None = None) -> dict | None:
    """启动追赶：另起一个线程补，不挡服务启动。没活可干就返回 None。"""
    with session_scope() as session:
        verdict = gaps(session, moment)
    if not needs_work(verdict):
        return None
    thread = threading.Thread(
        target=derive_pass, kwargs={"moment": moment}, name="lp-derive", daemon=True
    )
    thread.start()
    return {"days": _days_to_build(verdict)}


__all__ = [
    "derive_pass",
    "expected_quote_day",
    "gaps",
    "needs_work",
    "start_derive",
]
