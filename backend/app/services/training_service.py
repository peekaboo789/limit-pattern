"""AI 训练宽表（需求第十一、十五节）。

一行 = 一只股票的一个标签日 T。口径只有一条硬规矩：**预测 T 日只允许用 T-1 及更早的数据**。

* `label_date` = T，标签只从 T 日那一行 `daily_quotes` 取（`is_limit_up` 是同步当时
  由涨停判定器写下的结论，这里一律不重新推导——重推一遍就有了第二套涨停口径）；
* `cutoff_date` = T 前一个交易日，宽表里所有特征都止于这一天；
* `t1_* … t5_*` 是 T-1…T-5 五根 K 线的原始行情，`prior_lag_dates()` 内部再切一次
  `day < label_day`，调用方不小心把 T 日混进来也带不进特征；
* 技术指标在 cutoff 那根上**现算**（不落 `daily_features`），窗口不够就是 None；
* 命中 error 级质量异常的行**照样写**，但 `quality_passed=False` 并记下原因：
  丢掉就没人知道这只股票这天本可以进训练集。`matrix()` 默认只取通过的那些行。

T-5…T-1 只在库里留着的那段全市场行情（`daily_quotes` 的交易日）里数：保留 N 天的库，
最早能出训练行的标签日是第 6 个交易日，保留天数不到 6 就一行都出不来。全市场都凑不满的
那一天只记一笔日级跳过账（代码留空）；在行情范围内仍凑不满的个股（停牌、新上市）才按只记账。
`calc_bars` 里为算样本补拉的历史不参与回看——那份历史只有涨停票有，混进来训练集就成了涨停票专场。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.algorithms.indicators import (
    MIN_BARS_FOR_INDICATORS,
    compute_indicator_series,
    indicator_feature_names,
)
from backend.app.core.constants import TRAINING_LOOKBACK_TRADING_DAYS
from backend.app.core.logging import get_logger
from backend.app.models.tables import (
    DailyFeature,
    DailyQuote,
    DataQualityError,
    SkippedRecord,
    Stock,
    StockTrainingRow,
)
from backend.app.services.feature_service import (
    INDICATOR_WARMUP_TRADING_DAYS,
    add_skip,
    history_start_for,
    record_skip,
)
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.quality_service import ERROR as ERROR_SEVERITY, error_pairs
from backend.app.services.retention import quote_dates

logger = get_logger("training_service")

#: T-1…T-5 五根 K 线。第十一节写死了这段回看长度。
BAR_LAGS: tuple[int, ...] = tuple(range(1, TRAINING_LOOKBACK_TRADING_DAYS + 1))
BAR_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover_rate",
)

#: cutoff 日那一行 `daily_features` 里带进宽表的派生列。
#: 只允许写 `compute_day_features()` 真产出的键——`test_features_and_training.py`
#: 拿一条真实特征行逐个核这些名字，改了特征而忘了这里会当场红。
CUTOFF_DERIVED_FIELDS: tuple[str, ...] = (
    "close_change",
    "amplitude",
    "body_percent",
    "volume_ratio_5",
    "volume_ratio_10",
    "amount_change",
    "amount_ratio_5",
    "position_20",
    "position_60",
    "turnover_ratio_5",
)

#: 标签日之前至少要几根 K 线，才谈得上 T-5…T-1 这一排特征。
REQUIRED_PRIOR_BARS = max(BAR_LAGS)

#: 跳过与拦下的原因。
SKIP_NOT_IN_MASTER = "code_not_in_master"
SKIP_INSUFFICIENT_HISTORY = "insufficient_history_for_training"
SKIP_MARKET_HISTORY = "market_history_too_short"
BLOCK_QUALITY_ERROR = "quality_error"

REASON_LABELS = {
    SKIP_NOT_IN_MASTER: "代码不在股票主档里",
    SKIP_INSUFFICIENT_HISTORY: f"这只票在库里行情范围内不足 {REQUIRED_PRIOR_BARS} 根（停牌或新上市）",
    SKIP_MARKET_HISTORY: f"库里这一天之前不足 {REQUIRED_PRIOR_BARS} 个交易日的行情，全市场都取不满 T-5…T-1",
    BLOCK_QUALITY_ERROR: "标签日、cutoff 或 T-5…T-1 里有 error 级数据质量异常（行仍写入，quality_passed=False）",
}


def prior_lag_dates(ordered: Sequence[date], label_day: date, lags: Iterable[int] = BAR_LAGS) -> dict[int, date]:
    """T-1…T-5 分别是哪一天。`day < label_day` 在这里再切一次，不靠调用方自觉。"""
    before = sorted(day for day in ordered if day < label_day)
    result: dict[int, date] = {}
    for lag in lags:
        if len(before) >= lag:
            result[int(lag)] = before[-lag]
    return result


def column_names() -> list[str]:
    """宽表列名与顺序：五根 K 线 → cutoff 派生 → 指标。导出与 `matrix()` 共用这一份。"""
    names: list[str] = []
    for lag in BAR_LAGS:
        for field_name in BAR_FIELDS:
            names.append(f"t{lag}_{field_name}")
    names.extend(f"cutoff_{name}" for name in CUTOFF_DERIVED_FIELDS)
    names.extend(indicator_feature_names())
    return names


def label_values(row: DailyQuote) -> dict[str, object]:
    """标签列。`label_limit_up` 直接取 `is_limit_up`，不重新判一遍涨停。"""
    return {
        "label_limit_up": bool(row.is_limit_up),
        "label_limit_down": bool(row.is_limit_down),
        "label_change_percent": row.change_percent,
        "label_close": row.close,
        "label_pre_close": row.pre_close,
        "label_limit_up_percent": row.limit_up_percent,
    }


def _master_error_codes(session: Session) -> set[str]:
    """没有日期、按代码封的 error（比如名称与代码对不上）。

    `error_pairs()` 只回带日期的那些，按天删它会把主档删掉；但这类脏数据同样
    不该进训练集，所以在这里单独问一次。
    """
    rows = session.execute(
        select(DataQualityError.stock_code).where(
            DataQualityError.severity == ERROR_SEVERITY, DataQualityError.trade_date.is_(None)
        )
    ).scalars().all()
    return {code for code in rows if code}


def _tainted(
    blocked_pairs: set[tuple[str, date]], master_errors: set[str], code: str, days: set[date]
) -> str:
    if code in master_errors:
        return BLOCK_QUALITY_ERROR
    if any((code, day) in blocked_pairs for day in days):
        return BLOCK_QUALITY_ERROR
    return ""


def build_training(
    session: Session,
    start: date,
    end: date,
    *,
    codes: Sequence[str] | None = None,
    stats: dict,
    bars_by_code: dict[str, list] | None = None,
) -> dict:
    """构造区间内的训练宽表行。幂等：按 (代码, 标签日) upsert，不删别人的行。

    顺序有讲究：调用方必须**先跑质量检查再建表**。反过来的话，第一次构建会把
    当天刚入库的异常行当成干净数据吃进去。

    `bars_by_code` 是逐日重建时共享的那份全市场行情：读一遍用到底，
    每个交易日重读全库会把一个区间任务变成几十倍白费。
    """
    warmup_from = history_start_for(
        session, start, MIN_BARS_FOR_INDICATORS + INDICATOR_WARMUP_TRADING_DAYS
    )
    stats["warmup_from"] = warmup_from.isoformat()
    if bars_by_code is None:
        bars_by_code = load_bars_bulk(session, end)
    quotes = session.execute(
        select(DailyQuote).where(DailyQuote.trade_date >= start, DailyQuote.trade_date <= end)
    ).scalars().all()
    by_code: dict[str, dict[date, DailyQuote]] = {}
    for row in quotes:
        by_code.setdefault(row.stock_code, {})[row.trade_date] = row
    if codes:
        wanted = {str(code) for code in codes}
        by_code = {code: days for code, days in by_code.items() if code in wanted}

    master = {row.code: row.name for row in session.query(Stock.code, Stock.name)}
    blocked = error_pairs(session, end=end)
    master_errors = _master_error_codes(session)
    features_map = _feature_map(session, by_code, warmup_from, end)

    # 回看只认库里留着的那段全市场行情。`load_bars_bulk` 还会带出 `calc_bars` 的补拉历史，
    # 而那份历史只给「涨停过、要算样本」的票补过：拿它凑 T-5…T-1，训练集里就只剩
    # 涨停票，标签率被抬到六成，跟全市场一成出头的真实底数是两回事。
    market_days = quote_dates(session)
    coverage_start = market_days[0] if market_days else start
    label_days = sorted({day for days in by_code.values() for day in days})
    short_days = {
        day: len([item for item in market_days if item < day])
        for day in label_days
        if len([item for item in market_days if item < day]) < REQUIRED_PRIOR_BARS
    }
    _clear_stale(session, start=start, end=end, codes=codes, short_days=set(short_days))
    for day, have in sorted(short_days.items()):
        # 全市场都取不满的那一天只记一笔，不给四千多只票各记一笔同样的话。
        add_skip(stats, SKIP_MARKET_HISTORY, 1)
        _record_skip(
            session,
            code="",
            day=day,
            stage="training",
            reason=SKIP_MARKET_HISTORY,
            detail=(
                f"库里这一天之前只有 {have} 个交易日的行情，需要 {REQUIRED_PRIOR_BARS} 个；"
                f"保留天数至少 {REQUIRED_PRIOR_BARS + 1} 天才会出训练行"
            ),
        )

    for code in sorted(by_code):
        days = by_code[code]
        bars = [bar for bar in (bars_by_code.get(code) or []) if bar.trade_date >= coverage_start]
        ordered_days = sorted(bar.trade_date for bar in bars)
        if code not in master:
            add_skip(stats, SKIP_NOT_IN_MASTER, 1)
            _record_skip(session, code=code, day=end, stage="training", reason=SKIP_NOT_IN_MASTER)
            continue
        written = 0
        for label_day in sorted(days):
            if label_day in short_days:
                continue
            prior_days = [day for day in ordered_days if day < label_day]
            if len(prior_days) < REQUIRED_PRIOR_BARS:
                add_skip(stats, SKIP_INSUFFICIENT_HISTORY, 1)
                _record_skip(
                    session,
                    code=code,
                    day=label_day,
                    stage="training",
                    reason=SKIP_INSUFFICIENT_HISTORY,
                    detail=f"库里行情范围内标签日之前只有 {len(prior_days)} 根，需要 {REQUIRED_PRIOR_BARS} 根",
                )
                continue
            lag_days = prior_lag_dates(ordered_days, label_day)
            if len(lag_days) < REQUIRED_PRIOR_BARS:
                continue
            cutoff = lag_days[1]
            bar_by_day = {bar.trade_date: bar for bar in bars}
            values: dict[str, object] = {
                "label_date": label_day.isoformat(),
                "cutoff_date": cutoff.isoformat(),
            }
            for lag, day in lag_days.items():
                bar = bar_by_day.get(day)
                for field_name in BAR_FIELDS:
                    values[f"t{lag}_{field_name}"] = (
                        None if bar is None else getattr(bar, field_name, None)
                    )
            cutoff_features = features_map.get((code, cutoff.isoformat())) or {}
            for name in CUTOFF_DERIVED_FIELDS:
                values[f"cutoff_{name}"] = cutoff_features.get(name)
            indicator_bars = [bar for bar in bars if bar.trade_date <= cutoff]
            indicators = (
                compute_indicator_series(indicator_bars)[-1] if indicator_bars else {}
            )
            for name in indicator_feature_names():
                values[name] = indicators.get(name)
            blocked_reason = _tainted(
                blocked, master_errors, code, {label_day, cutoff, *lag_days.values()}
            )
            _upsert(
                session,
                code=code,
                label_day=label_day,
                cutoff_day=cutoff,
                label=days[label_day],
                values=values,
                quality_passed=blocked_reason == "",
                reason=blocked_reason,
            )
            written += 1
            stats["training_count"] += 1
            if blocked_reason:
                stats["training_blocked"] = stats.get("training_blocked", 0) + 1
        if written:
            stats["stock_count"] += 1
        if (stats["stock_count"] % 400) == 0:
            session.flush()
    # 循环里的批量 flush 只覆盖到 400 的整数倍，尾巴上那几十只票还压在 session 里。
    # 会话没开 autoflush，同一次作业里随后再查这张表就会看见「行明明建了、矩阵是空的」，
    # 所以收尾必须补一次：写入在事务里对后续读取可见，提交仍由调用方决定。
    session.flush()
    return stats


def _clear_stale(
    session: Session,
    *,
    start: date,
    end: date,
    codes: Sequence[str] | None,
    short_days: set[date],
) -> None:
    """重建前先把这段区间里旧的训练跳过账、以及全市场都凑不满那几天的旧行清掉。

    旧口径会拿补拉历史给涨停票凑出训练行，并给其余四千多只各记一笔「只有 4 根」：
    不清的话新旧两套口径并排留在库里，页面上的数也就对不上这一轮跑出来的结果。
    """
    skips = session.query(SkippedRecord).filter(
        SkippedRecord.stage == "training",
        SkippedRecord.trade_date >= start,
        SkippedRecord.trade_date <= end,
    )
    if codes:
        skips = skips.filter(SkippedRecord.stock_code.in_([str(code) for code in codes]))
    skips.delete(synchronize_session=False)
    if short_days:
        rows = session.query(StockTrainingRow).filter(StockTrainingRow.label_date.in_(sorted(short_days)))
        if codes:
            rows = rows.filter(StockTrainingRow.stock_code.in_([str(code) for code in codes]))
        rows.delete(synchronize_session=False)


def _feature_map(
    session: Session, by_code: dict[str, dict[date, DailyQuote]], low: date, high: date
) -> dict[tuple[str, str], dict]:
    """一次把区间内的单日特征取回来。逐只查会打成千上万趟 SQL。"""
    codes = list(by_code)
    if not codes:
        return {}
    rows = session.query(DailyFeature).filter(
        DailyFeature.stock_code.in_(codes),
        DailyFeature.trade_date >= low,
        DailyFeature.trade_date <= high,
    ).all()
    return {(row.stock_code, row.trade_date.isoformat()): dict(row.features or {}) for row in rows}


def _upsert(
    session: Session,
    *,
    code: str,
    label_day: date,
    cutoff_day: date,
    label: DailyQuote,
    values: dict,
    quality_passed: bool,
    reason: str,
) -> None:
    row = session.scalar(
        select(StockTrainingRow).where(
            StockTrainingRow.stock_code == code, StockTrainingRow.label_date == label_day
        )
    )
    if row is None:
        row = StockTrainingRow(stock_code=code, label_date=label_day)
        session.add(row)
    payload = dict(label_values(label))
    payload.update(values)
    row.cutoff_date = cutoff_day
    row.label_limit_up = bool(label.is_limit_up)
    row.quality_passed = quality_passed
    row.reason = reason
    row.features = payload


def _record_skip(
    session: Session, *, code: str, day: date, stage: str, reason: str, detail: str = ""
) -> None:
    record_skip(session, day=day, stock_code=code, stage=stage, reason=reason, detail=detail)


def matrix(
    session: Session,
    start: date,
    end: date,
    *,
    only_passed: bool = True,
    label_limit_up: bool | None = None,
    codes: Sequence[str] | None = None,
    limit: int | None = None,
) -> dict:
    """把区间内的训练行摊成 `{columns, rows}`——导出与档4 的训练脚本吃同一份。"""
    stmt = select(StockTrainingRow).where(
        StockTrainingRow.label_date >= start, StockTrainingRow.label_date <= end
    )
    if only_passed:
        stmt = stmt.where(StockTrainingRow.quality_passed.is_(True))
    if label_limit_up is not None:
        stmt = stmt.where(StockTrainingRow.label_limit_up.is_(label_limit_up))
    if codes:
        stmt = stmt.where(StockTrainingRow.stock_code.in_(list(codes)))
    rows = list(session.scalars(stmt.order_by(StockTrainingRow.label_date, StockTrainingRow.stock_code)).all())
    columns = ["stock_code", "label_date", "cutoff_date", "label_limit_up", "quality_passed"]
    columns += column_names()
    body = []
    for row in rows:
        features = dict(row.features or {})
        line = [row.stock_code, row.label_date.isoformat(), row.cutoff_date.isoformat(), bool(row.label_limit_up), bool(row.quality_passed)]
        line += [features.get(name) for name in column_names()]
        body.append(line)
        if limit is not None and len(body) >= int(limit):
            break
    return {
        "columns": columns,
        "rows": body,
        "total": len(body),
        "only_passed": only_passed,
        "disclaimer": "这是历史行情的特征与标签，不是对未来收益的预测。",
    }


def training_counts(session: Session) -> dict:
    """库里训练行的账：多少行、多少被质量闸门挡住、覆盖哪几天。"""
    total = int(session.query(StockTrainingRow).count() or 0)
    blocked = int(
        session.query(StockTrainingRow).filter(StockTrainingRow.quality_passed.is_(False)).count() or 0
    )
    latest = session.scalar(select(StockTrainingRow.label_date).order_by(StockTrainingRow.label_date.desc()).limit(1))
    oldest = session.scalar(select(StockTrainingRow.label_date).order_by(StockTrainingRow.label_date).limit(1))
    return {
        "rows": total,
        "blocked_rows": blocked,
        "earliest_label_date": oldest.isoformat() if oldest else "",
        "latest_label_date": latest.isoformat() if latest else "",
        "reason_labels": REASON_LABELS,
    }


def skip_reason_labels() -> dict[str, str]:
    return dict(REASON_LABELS)


__all__ = [
    "BAR_FIELDS",
    "BAR_LAGS",
    "BLOCK_QUALITY_ERROR",
    "CUTOFF_DERIVED_FIELDS",
    "REASON_LABELS",
    "REQUIRED_PRIOR_BARS",
    "SKIP_INSUFFICIENT_HISTORY",
    "SKIP_MARKET_HISTORY",
    "SKIP_NOT_IN_MASTER",
    "build_training",
    "column_names",
    "label_values",
    "matrix",
    "prior_lag_dates",
    "skip_reason_labels",
    "training_counts",
]
