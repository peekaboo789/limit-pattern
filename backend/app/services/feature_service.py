"""单日特征落库（需求第五~十三、三十六节）。

`daily_features` 存的是**一根 K 线自己那天**的特征：由
`algorithms.features.compute_day_features()` 算出，一行一只股票一天，JSON 里既有
原始 OHLCV，也有那 20 项单日形态特征。三日窗口与向量不吃这张表——它们直接吃行情。
这张表落地后的读取方只有两处：训练宽表取 cutoff 那一行的派生列，数据管理页数列数。
涨停形态页上「这一天到底长什么样」吃的是样本行里存好的 `day_details`，不回来这张表重读：
两者都是同一把尺子在那一天算出的快照，但页面要展示的是「落库那一版」。

技术指标**不在**这张表里（老工程也是这个口径）：指标要 60 根才齐，落库会把区间头几天
那一大片 None 常驻在库里，而真正需要指标的只有训练宽表，由 `training_service` 现算。

第 i 天的任何一列只由 `bars[: i + 1]` 决定——这是第十二节「禁止未来数据」在这张表上的
具体实现，`test_features_and_training.py` 里有一条截尾不变的断言盯着它。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from sqlalchemy.orm import Session

from backend.app.algorithms.features import compute_day_features
from backend.app.core.constants import FULL_HISTORY_TRADING_DAYS
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyFeature, SkippedRecord
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.retention import quote_dates

logger = get_logger("feature_service")

#: 载入历史的长度：`position_60` 与 `volume_ratio_10` 需要窗口前面那些日子。
#: 明细只留约一周时这个预热取不满，特征仍然有值（`compute_day_features` 按已有天数算），
#: 但 `position_60` 会退化成「按这几根算」——这一点在数据管理页要说得出来。
HISTORY_LOOKBACK_TRADING_DAYS = FULL_HISTORY_TRADING_DAYS

#: 指标预热：MACD/RSI/ATR 要 60 根才齐，再留 20 根余量给停牌与窗口边界。
#: 只有训练宽表用它（指标不落 `daily_features`）。
INDICATOR_WARMUP_TRADING_DAYS = 20

#: 跳过原因。写进 `skipped_records`，数据管理页按原因分组显示。
SKIP_NO_BAR = "no_bar"

#: 单日特征 JSON 里必须有这些键，缺一个就是向量与库里对不上。
REQUIRED_FEATURE_KEYS = (
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "turnover_rate",
    "close_change",
    "volume_ratio_5",
    "position_20",
)


def history_start_for(session: Session, day: date, trading_days: int) -> date:
    """`day` 往前数 `trading_days` 个交易日是哪天。日历不够长就取库里最早那天。

    用行情日期而不是自然日：区间要覆盖的是「N 根 K 线」，按自然日减会把长假
    算成七天，春节那一段直接少二十来根预热。
    """
    days = [item for item in quote_dates(session) if item <= day]
    if not days:
        return day
    index = max(len(days) - int(trading_days), 0)
    return days[index]


def features_for_range(
    bars: Sequence,
    start: date,
    end: date,
) -> list[tuple[date, dict]]:
    """一段升序行情 → 区间内逐日的特征行。窗口之前的日子只用来喂热，不出现在结果里。"""
    ordered = sorted({bar.trade_date: bar for bar in bars}.values(), key=lambda bar: bar.trade_date)
    payloads: list[tuple[date, dict]] = []
    for index, bar in enumerate(ordered):
        if not (start <= bar.trade_date <= end):
            continue
        if bar.is_partial:
            # 半天线不当作一天的收盘特征：它的量能只有一上午。
            continue
        payloads.append((bar.trade_date, compute_day_features(list(ordered[: index + 1]))))
    return payloads


def store_day_feature(session: Session, stock_code: str, day: date, features: dict) -> None:
    """按 (代码, 日期) upsert 一行。重跑不产生第二行，也不留旧口径的残值。"""
    row = (
        session.query(DailyFeature)
        .filter(DailyFeature.stock_code == stock_code, DailyFeature.trade_date == day)
        .one_or_none()
    )
    if row is None:
        row = DailyFeature(stock_code=stock_code, trade_date=day)
        session.add(row)
    row.features = dict(features)


def record_skip(
    session: Session,
    *,
    day: date,
    stock_code: str,
    stage: str,
    reason: str,
    detail: str = "",
) -> None:
    """记一条跳过账。同一只股票同一阶段同一原因只留一条，重复重建不会把账刷爆。"""
    exists = (
        session.query(SkippedRecord)
        .filter(
            SkippedRecord.trade_date == day,
            SkippedRecord.stock_code == stock_code,
            SkippedRecord.stage == stage,
            SkippedRecord.reason == reason,
        )
        .one_or_none()
    )
    if exists is not None:
        if detail and exists.detail != detail:
            exists.detail = detail[:200]
        return
    session.add(
        SkippedRecord(
            trade_date=day,
            stock_code=stock_code,
            stage=stage,
            reason=reason,
            detail=detail[:200],
        )
    )


def rebuild_features(
    session: Session,
    start: date,
    end: date,
    *,
    codes: Sequence[str] | None = None,
    stats: dict | None = None,
    bars_by_code: dict[str, list] | None = None,
) -> dict:
    """区间内逐日重算单日特征。预热锚在**区间首日**，不是今天。

    锚在今天的后果是「重建上周」时上周第一天的 `position_20` 只剩几天可看：
    预热必须跟着区间走，重跑十次得到的是同一批值。

    `bars_by_code` 让调用方把全市场那份行情只读一遍（逐日重建时每天重读全库
    是几十倍白费），传进来就用，不传自己读。
    """
    stats = stats if stats is not None else new_stats("rebuild_features", start, end)
    warmup_from = history_start_for(session, start, HISTORY_LOOKBACK_TRADING_DAYS)
    if bars_by_code is None:
        bars_by_code = load_bars_bulk(session, end)
    universe = sorted({code for code, bars in bars_by_code.items() if bars and _touches(bars, start, end)})
    if codes:
        wanted = {str(code) for code in codes}
        universe = [code for code in universe if code in wanted]
        missing = sorted(wanted - set(universe))
        for code in missing:
            record_skip(
                session,
                day=end,
                stock_code=code,
                stage="feature",
                reason=SKIP_NO_BAR,
                detail=f"{start}~{end} 区间内没有可用行情",
            )
            stats["skipped"][SKIP_NO_BAR] = stats["skipped"].get(SKIP_NO_BAR, 0) + 1
    stats["warmup_from"] = warmup_from.isoformat()
    for index, code in enumerate(universe):
        bars = bars_by_code.get(code) or []
        payloads = features_for_range(bars, start, end)
        if not payloads:
            record_skip(
                session,
                day=end,
                stock_code=code,
                stage="feature",
                reason=SKIP_NO_BAR,
                detail="区间内没有可用的收盘K线",
            )
            stats["skipped"][SKIP_NO_BAR] = stats["skipped"].get(SKIP_NO_BAR, 0) + 1
            continue
        for day, features in payloads:
            store_day_feature(session, code, day, features)
            stats["feature_count"] += 1
            stats["date_range"].add(day.isoformat())
        stats["stock_count"] += 1
        if (index + 1) % 500 == 0:
            session.flush()
    # 收尾补一次 flush：会话没开 autoflush，同一次作业里后面还要按这张表回读
    # （训练宽表取 cutoff 那几列、数据管理页数落库条数），不冲下去就查到自己没写完的空账。
    session.flush()
    stats["universe_count"] = len(universe)
    stats["date_range"] = sorted(stats["date_range"])
    return stats


def _touches(bars: Sequence, start: date, end: date) -> bool:
    return any(start <= bar.trade_date <= end and not bar.is_partial for bar in bars)


def new_stats(job: str, start: date, end: date) -> dict:
    return {
        "job": job,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "stock_count": 0,
        "feature_count": 0,
        "pattern_count": 0,
        "match_count": 0,
        "summary_count": 0,
        "target_count": 0,
        "training_count": 0,
        "limit_up_count": 0,
        "universe_count": 0,
        "pool_size": 0,
        "warmup_from": "",
        "date_range": set(),
        "skipped": {},
        "skipped_total": 0,
    }


def add_skip(stats: dict, reason: str, count: int = 1) -> None:
    stats["skipped"][reason] = stats["skipped"].get(reason, 0) + count
    stats["skipped_total"] = sum(stats["skipped"].values())


def public_stats(stats: dict) -> dict:
    """把内部记账（那个 set）折成能进 JSON 响应的一份账。"""
    payload = dict(stats)
    range_value = payload.get("date_range")
    if isinstance(range_value, set):
        payload["date_range"] = sorted(range_value)
    return payload


def skip_page(
    session: Session,
    *,
    stage: str | None = None,
    reason: str | None = None,
    day: date | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """跳过记录分页 + 按原因计数。数据管理页要能回答「到底跳过了什么」。"""
    query = session.query(SkippedRecord)
    counting = session.query(SkippedRecord.reason)
    if stage:
        query = query.filter(SkippedRecord.stage == stage)
        counting = counting.filter(SkippedRecord.stage == stage)
    if reason:
        query = query.filter(SkippedRecord.reason == reason)
    if day:
        query = query.filter(SkippedRecord.trade_date == day)
        counting = counting.filter(SkippedRecord.trade_date == day)
    by_reason: dict[str, int] = {}
    for row in counting.all():
        by_reason[row.reason] = by_reason.get(row.reason, 0) + 1
    rows = (
        query.order_by(SkippedRecord.trade_date.desc(), SkippedRecord.stock_code)
        .limit(int(limit))
        .offset(int(offset))
        .all()
    )
    return {
        "total": sum(by_reason.values()),
        "by_reason": dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))),
        "items": [
            {
                "trade_date": row.trade_date.isoformat() if row.trade_date else "",
                "stock_code": row.stock_code,
                "stage": row.stage,
                "reason": row.reason,
                "detail": row.detail,
            }
            for row in rows
        ],
    }


__all__ = [
    "HISTORY_LOOKBACK_TRADING_DAYS",
    "INDICATOR_WARMUP_TRADING_DAYS",
    "REQUIRED_FEATURE_KEYS",
    "SKIP_NO_BAR",
    "add_skip",
    "features_for_range",
    "history_start_for",
    "new_stats",
    "public_stats",
    "record_skip",
    "rebuild_features",
    "skip_page",
    "store_day_feature",
]
