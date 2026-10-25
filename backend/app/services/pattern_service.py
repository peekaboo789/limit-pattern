"""涨停样本库（需求第三、八、十五节）。

样本 = 某个交易日实际涨停的股票；每个样本存它**涨停之前**三个交易日的形态向量。
涨停当日只贡献标签，绝不进向量——`build_three_day_window(..., include_as_of_date=False)`
的严格口径保证这件事。

这张表里的向量必须由与首页榜、盘中榜、回测**同一条路径**产出：
`algorithms.features.build_three_day_window` + `algorithms.similarity.score_components`
是唯一的尺子，落库只是把那把尺子的结果存下来，不是第二把尺子。
`test_patterns_and_matches.py` 里有一条断言逐维比较库里那一条与现算那一条，
谁改了口径当场炸。

样本池的宽度就是设置里的「对比窗口」（`config.compare_window_days()`，默认 3 个交易日），不做第二个数：
分档阈值、榜上的样本条数、回测的池子都是按这个数定的，多一个数就多一套对不上的账。

第十五节的闸门也在这里：涨停当日或形态三天任一天有 error 级质量异常，这只股票当天
不进样本库，并按 `data_quality_error` 记一条跳过原因。闸门放在写入**之前**，
被判定不可信的行情连 `daily_features` 都不该留。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.algorithms.features import (
    AGGREGATE_NAMES,
    DAILY_SPEC,
    VECTOR_DIM,
    build_three_day_window,
    raw_day,
)
from backend.app.core.constants import MIN_LISTED_TRADING_DAYS
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyQuote, LimitUpPattern, SkippedRecord, Stock
from backend.app.providers.markets import is_in_scope
from backend.app.services.feature_service import add_skip, record_skip
from backend.app.services.history_backfill import ensure_prior_bars, load_bars, load_bars_bulk
from backend.app.services.quality_service import error_pairs
from backend.app.services.retention import quote_dates
from backend.app.services.similarity_service import DISCLAIMER, pool_dates

logger = get_logger("pattern_service")

#: 跳过原因：命中 error 级数据质量异常。与「凑不齐三天」区分开——
#: 那一类是算不了，这一类是算得出但不该用。
SKIP_QUALITY_ERROR = "data_quality_error"
SKIP_INSUFFICIENT_WINDOW = "insufficient_window"

#: 跳过原因给页面看的说法。
REASON_LABELS = {
    SKIP_QUALITY_ERROR: "涨停日或形态三天里有 error 级数据质量异常",
    SKIP_INSUFFICIENT_WINDOW: "库里凑不齐涨停日之前那三根K线",
}


def stock_names(session: Session) -> dict[str, str]:
    return {row.code: row.name for row in session.query(Stock.code, Stock.name)}


def limit_up_rows(session: Session, day: date) -> list[DailyQuote]:
    return list(
        session.scalars(
            select(DailyQuote)
            .where(DailyQuote.trade_date == day, DailyQuote.is_limit_up.is_(True))
            .order_by(DailyQuote.stock_code)
        ).all()
    )


def build_patterns(
    session: Session,
    day: date,
    *,
    bars_by_code: dict[str, list] | None = None,
    dirty: set[tuple[str, date]] | None = None,
    stats: dict,
) -> dict:
    """重建某一天的涨停样本库（幂等：先删当天再写入）。

    样本行不存股票名称：名称一律从 `stocks` 读（榜、回测、预测池都是这个来源），
    抄一份进样本表就有两套名字，改名之后哪套算数没人说得清。
    """
    if bars_by_code is None:
        bars_by_code = load_bars_bulk(session, day)
    if dirty is None:
        dirty = error_pairs(session, end=day)

    rows = limit_up_rows(session, day)
    candidates = sorted({row.stock_code for row in rows if is_in_scope(row.stock_code)})
    stats["limit_up_count"] = len(candidates)

    _delete_day(session, day)
    for code in candidates:
        bars = bars_by_code.get(code) or []
        window = build_three_day_window(bars, day, include_as_of_date=False)
        backfill_reason = None
        if not window.ok:
            # 和相似度页的样本池同一个口径：涨停日前不足三根先补拉（只拉涨停日之前的线），
            # 否则保留窗口最前面那几天的涨停，那边进池、这边落库时却被当成「凑不齐」跳过。
            backfill_reason = ensure_prior_bars(session, code, day, day)
            if backfill_reason is None:
                bars = load_bars(session, code, day)
                window = build_three_day_window(bars, day, include_as_of_date=False)
        if not window.ok:
            record_skip(
                session,
                day=day,
                stock_code=code,
                stage="pattern",
                reason=SKIP_INSUFFICIENT_WINDOW,
                detail=backfill_reason
                or f"涨停日之前只有 {sum(1 for bar in bars if bar.trade_date < day)} 根可用K线，需要 {MIN_LISTED_TRADING_DAYS} 根",
            )
            add_skip(stats, SKIP_INSUFFICIENT_WINDOW)
            continue
        window_days = [bar.trade_date for bar in window.days]
        hit = next((item for item in [day, *window_days] if (code, item) in dirty), None)
        if hit is not None:
            record_skip(
                session,
                day=day,
                stock_code=code,
                stage="pattern",
                reason=SKIP_QUALITY_ERROR,
                detail=f"{hit.isoformat()} 有 error 级数据质量异常",
            )
            add_skip(stats, SKIP_QUALITY_ERROR)
            continue
        session.add(
            LimitUpPattern(
                stock_code=code,
                limit_up_date=day,
                window_dates=[item.isoformat() for item in window_days],
                feature_vector=[float(value) for value in window.vector],
                day_details=[day_detail(item) for item in window.day_features],
            )
        )
        stats["pattern_count"] += 1
    session.flush()
    return stats


def _delete_day(session: Session, day: date) -> None:
    session.query(LimitUpPattern).filter(LimitUpPattern.limit_up_date == day).delete(
        synchronize_session=False
    )


def pool_pattern_dates(session: Session, as_of: date) -> list[date]:
    """样本池覆盖的交易日：`as_of` 及其前「对比窗口 − 1」个交易日。

    与 `similarity_service.pool_dates` 同一个来源，不另算一份。
    """
    return pool_dates(session, as_of)


def pool_patterns(session: Session, as_of: date) -> tuple[list[date], list[LimitUpPattern]]:
    """库里已落库的样本池。样本表为空时返回空列表——不是拿现算的池子冒充。"""
    dates = pool_pattern_dates(session, as_of)
    if not dates:
        return [], []
    rows = list(
        session.scalars(
            select(LimitUpPattern)
            .where(LimitUpPattern.limit_up_date.in_(dates))
            .order_by(LimitUpPattern.limit_up_date, LimitUpPattern.stock_code)
        ).all()
    )
    return dates, rows


def pattern_row(session: Session, code: str, limit_date: date) -> LimitUpPattern | None:
    return session.scalar(
        select(LimitUpPattern)
        .where(LimitUpPattern.stock_code == code, LimitUpPattern.limit_up_date == limit_date)
        .limit(1)
    )


def latest_pattern_date(session: Session) -> date | None:
    return session.scalar(select(LimitUpPattern.limit_up_date).order_by(LimitUpPattern.limit_up_date.desc()).limit(1))


def pattern_to_dict(row: LimitUpPattern, names: dict[str, str], *, with_details: bool = False) -> dict:
    payload = {
        "code": row.stock_code,
        "name": names.get(row.stock_code, ""),
        "limit_date": row.limit_up_date.isoformat() if row.limit_up_date else "",
        "window": list(row.window_dates or []),
        "dimension": len(row.feature_vector or []),
    }
    if with_details:
        payload["days"] = list(row.day_details or [])
        payload["vector"] = [float(value) for value in (row.feature_vector or [])]
    return payload


def day_detail(features: dict) -> dict:
    """样本一天的存档：原始行情 + 那 20 项单日形态特征。

    两者都存，是因为页面上「这一天涨了几个点、量比前五天大不大」要分开看；
    向量本身不在这里重复——它已经在 `feature_vector` 那一列，从向量反推是另一把尺子。
    """
    return {
        "trade_date": features.get("trade_date"),
        "quote": raw_day(features),
        "features": {name: features.get(name) for name, _group in DAILY_SPEC},
    }


def day_feature_names() -> list[str]:
    """单日特征的名字与顺序，与 `DAILY_SPEC` 一字不差。前端表头读它，别自己写一遍。"""
    return [name for name, _group in DAILY_SPEC]


def aggregates_of(row: LimitUpPattern) -> dict[str, float] | None:
    """三日聚合从向量尾部反查名字——向量的形状与特征计算之间由 `VECTOR_DIM` 钉死。

    长度对不上时返回 None 而不是截断补零：那说明这一行是旧口径写的，
    拿它算出来的聚合值不能和现在榜上的数放在一起看。
    """
    vector = [float(value) for value in (row.feature_vector or [])]
    if len(vector) != VECTOR_DIM:
        return None
    return dict(zip(AGGREGATE_NAMES, vector[len(DAILY_SPEC) * 3 :]))


def vector_of(row: LimitUpPattern) -> list[float]:
    return [float(value) for value in (row.feature_vector or [])]


def overview(session: Session, as_of: date) -> dict:
    """涨停形态页那份账：样本池这几天库里到底存了哪些样本。

    读的是 `limit_up_patterns`，不是现算的涨停池。这两份数据在库里可以不一致
    （样本没重建、当天有 error 被闸门挡了、行情不足三天），而页面要展示的正是
    「库里有的那一版」——否则用户点下重建之后数字变了，等于页面之前报的是个不存在数。
    """
    dates = pool_pattern_dates(session, as_of)
    names = stock_names(session)
    rows = (
        []
        if not dates
        else list(
            session.scalars(
                select(LimitUpPattern)
                .where(LimitUpPattern.limit_up_date.in_(dates))
                .order_by(LimitUpPattern.limit_up_date.desc(), LimitUpPattern.stock_code)
            ).all()
        )
    )
    quotes = _limit_day_quotes(session, dates)
    skips = _skip_totals(session, dates)
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        day = row.limit_up_date.isoformat()
        item = pattern_to_dict(row, names)
        quote = quotes.get((row.stock_code, day))
        item["close"] = quote.close if quote else None
        item["change_percent"] = quote.change_percent if quote else None
        item["limit_up_percent"] = quote.limit_up_percent if quote else None
        grouped.setdefault(day, []).append(item)
    days = [
        {
            "limit_date": day.isoformat(),
            "count": len(grouped.get(day.isoformat()) or []),
            "skipped": skips.get(day.isoformat(), {}),
            "items": grouped.get(day.isoformat()) or [],
        }
        for day in sorted(dates, reverse=True)
    ]
    return {
        "trade_date": as_of.isoformat(),
        "pool_dates": [day.isoformat() for day in dates],
        "pool_is_current": pool_is_current(session, as_of),
        "sample_total": len(rows),
        "days": days,
        "feature_names": day_feature_names(),
        "aggregate_names": list(AGGREGATE_NAMES),
        "dimension": VECTOR_DIM,
        "reason_labels": dict(REASON_LABELS),
        "disclaimer": DISCLAIMER,
    }


def samples_for_code(session: Session, code: str, *, with_details: bool = True) -> dict:
    """一只股票在保留窗口里落库的全部涨停样本。样本按涨停日倒序。

    带明细时把 67 维向量的聚合项也摊开：页面上「三日涨幅、量能趋势」这些数
    要能指回向量尾部那七维，而不是由前端再算一遍。
    """
    rows = list(
        session.scalars(
            select(LimitUpPattern)
            .where(LimitUpPattern.stock_code == code)
            .order_by(LimitUpPattern.limit_up_date.desc())
        ).all()
    )
    names = stock_names(session)
    quotes = {
        row.trade_date.isoformat(): row
        for row in session.query(DailyQuote).filter(
            DailyQuote.stock_code == code,
            DailyQuote.trade_date.in_([item.limit_up_date for item in rows]),
        )
    }
    items = []
    for row in rows:
        payload = pattern_to_dict(row, names, with_details=with_details)
        day = payload["limit_date"]
        quote = quotes.get(day)
        payload["close"] = quote.close if quote else None
        payload["change_percent"] = quote.change_percent if quote else None
        payload["is_limit_up"] = bool(quote.is_limit_up) if quote else None
        payload["aggregates"] = aggregates_of(row)
        items.append(payload)
    return {
        "code": code,
        "name": names.get(code, ""),
        "samples": items,
        "feature_names": day_feature_names(),
        "disclaimer": DISCLAIMER,
    }


def _limit_day_quotes(session: Session, days: list[date]) -> dict[tuple[str, str], DailyQuote]:
    if not days:
        return {}
    rows = session.query(DailyQuote).filter(DailyQuote.trade_date.in_(days)).all()
    return {(row.stock_code, row.trade_date.isoformat()): row for row in rows}


def _skip_totals(session: Session, days: list[date]) -> dict[str, dict[str, int]]:
    """这几天各有多少条被闸门挡在外面、为什么。数字来自 `skipped_records`。"""
    if not days:
        return {}
    rows = (
        session.query(SkippedRecord.trade_date, SkippedRecord.reason, func.count(SkippedRecord.id))
        .filter(SkippedRecord.stage == "pattern", SkippedRecord.trade_date.in_(days))
        .group_by(SkippedRecord.trade_date, SkippedRecord.reason)
        .all()
    )
    totals: dict[str, dict[str, int]] = {}
    for day, reason, count in rows:
        if day is None:
            continue
        totals.setdefault(day.isoformat(), {})[str(reason)] = int(count)
    return totals


def pool_is_current(session: Session, as_of: date) -> bool:
    """样本库是否覆盖到了这个池子该有样本的那几天。

    只回答「这几天里有没有任何一行样本」，不回答「齐不齐」：齐不齐由
    `build_patterns` 的 limit_up_count 与 pattern_count 之差说明。
    """
    dates = pool_pattern_dates(session, as_of)
    if not dates:
        return False
    count = (
        session.query(LimitUpPattern)
        .filter(LimitUpPattern.limit_up_date.in_(dates))
        .count()
    )
    return int(count) > 0


__all__ = [
    "REASON_LABELS",
    "SKIP_INSUFFICIENT_WINDOW",
    "SKIP_QUALITY_ERROR",
    "aggregates_of",
    "build_patterns",
    "day_detail",
    "day_feature_names",
    "latest_pattern_date",
    "overview",
    "pattern_row",
    "pattern_to_dict",
    "pool_is_current",
    "pool_pattern_dates",
    "pool_patterns",
    "samples_for_code",
    "stock_names",
    "vector_of",
]
