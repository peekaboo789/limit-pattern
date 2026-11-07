"""67 维形态向量，按旧版《前 100 行是怎么挑出来的》第五节逐项实现。

每天 20 个特征 × 3 天（T-3、T-2、T-1）= 60 维，加 7 个三日汇总 = 67 维。
缺数的特征（新浪没有成交额/换手、没有前一天可比等）写 NaN，标准化之后按池中位数填，
即标准化后为 0，不表达偏离（修正旧版文档第八节第 5 条指出的「先填 0 再标准化」问题）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from backend.app.core.constants import GROUP_WEIGHTS, MIN_LISTED_TRADING_DAYS

NAN = float("nan")

# (字段, 组, 组内权重)。顺序即向量里的顺序。
DAILY_SPEC: list[tuple[str, str, float]] = [
    ("open_change", "price_trend", 0.15),
    ("high_change", "price_trend", 0.25),
    ("low_change", "price_trend", 0.20),
    ("close_change", "price_trend", 0.40),
    ("amplitude", "kline_structure", 0.25),
    ("body_length", "kline_structure", 0.25),
    ("upper_shadow", "kline_structure", 0.25),
    ("lower_shadow", "kline_structure", 0.25),
    ("volume_change", "volume", 1 / 3),
    ("volume_ratio_5", "volume", 1 / 3),
    ("volume_ratio_10", "volume", 1 / 3),
    ("amount_change", "amount", 1 / 3),
    ("amount_ratio_5", "amount", 1 / 3),
    ("amount_ratio_10", "amount", 1 / 3),
    ("turnover_rate", "turnover", 0.5),
    ("turnover_change", "turnover", 0.5),
    ("position_5", "position", 0.25),
    ("position_10", "position", 0.25),
    ("position_20", "position", 0.25),
    ("position_60", "position", 0.25),
]
AGGREGATE_NAMES = [
    "three_day_return",
    "volume_growth",
    "amount_growth",
    "turnover_change_3d",
    "close_trend",
    "volume_trend",
    "amount_trend",
]
DAY_LABELS = ("T-3", "T-2", "T-1")
VECTOR_DIM = len(DAILY_SPEC) * 3 + len(AGGREGATE_NAMES)
GROUPS = ("price_trend", "kline_structure", "volume", "amount", "turnover", "position", "three_day_trend")


@dataclass
class Bar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float | None = None
    volume: float | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    is_partial: bool = False


@dataclass
class WindowResult:
    ok: bool
    reason: str = ""
    days: list[Bar] = field(default_factory=list)
    day_features: list[dict] = field(default_factory=list)
    vector: list[float] = field(default_factory=list)


def feature_names() -> list[str]:
    names: list[str] = []
    for offset in ("t3", "t2", "t1"):
        for name, _group, _inner in DAILY_SPEC:
            names.append(f"{offset}_{name}")
    names.extend(AGGREGATE_NAMES)
    return names


def feature_groups() -> list[str]:
    groups = [group for _n, group, _i in DAILY_SPEC] * 3
    return groups + ["three_day_trend"] * len(AGGREGATE_NAMES)


def dimension_weights(group_weights: dict[str, float] | None = None) -> list[float]:
    """逐维权重 = 组权重 × 组内权重；先按组归一（每组合计恰好等于组权重），再整体归一。"""
    groups = dict(GROUP_WEIGHTS if group_weights is None else group_weights)
    raw = [groups[group] * inner for _n, group, inner in DAILY_SPEC] * 3
    raw += [groups["three_day_trend"] * (1 / len(AGGREGATE_NAMES))] * len(AGGREGATE_NAMES)
    labels = feature_groups()
    sums: dict[str, float] = {}
    for weight, group in zip(raw, labels):
        sums[group] = sums.get(group, 0.0) + weight
    per_group = [weight / sums[group] * groups[group] for weight, group in zip(raw, labels)]
    total = sum(per_group)
    return [weight / total for weight in per_group]


def _pct(price: float | None, base: float | None) -> float:
    if price is None or base is None or base <= 0:
        return NAN
    return (price / base - 1.0) * 100.0


def _position(history: list[Bar], window: int) -> float:
    chunk = history[-window:]
    lo = min(bar.low for bar in chunk)
    hi = max(bar.high for bar in chunk)
    if hi <= lo:
        return 0.5
    return (chunk[-1].close - lo) / (hi - lo)


def _ratio(current: float | None, previous: list[float | None]) -> float:
    values = [value for value in previous if value is not None and value > 0]
    if current is None or not values:
        return NAN
    return current / (sum(values) / len(values))


def _change(current: float | None, previous: float | None) -> float:
    if current is None or previous is None or previous <= 0:
        return NAN
    return (current / previous - 1.0) * 100.0


def compute_day_features(history: list[Bar]) -> dict:
    """history 最后一根是当天。"""
    today = history[-1]
    base = today.pre_close if today.pre_close and today.pre_close > 0 else (history[-2].close if len(history) >= 2 else None)
    prev = history[:-1]
    prev_bar = prev[-1] if prev else None
    hi_body, lo_body = max(today.open, today.close), min(today.open, today.close)
    feats = {
        "trade_date": today.trade_date.isoformat(),
        "open": today.open,
        "high": today.high,
        "low": today.low,
        "close": today.close,
        "pre_close": base,
        "volume": today.volume,
        "amount": today.amount,
        "turnover_rate_raw": today.turnover_rate,
        "is_partial": today.is_partial,
        "open_change": _pct(today.open, base),
        "high_change": _pct(today.high, base),
        "low_change": _pct(today.low, base),
        "close_change": _pct(today.close, base),
        "amplitude": (today.high - today.low) / base * 100 if base else NAN,
        "body_length": abs(today.close - today.open) / base * 100 if base else NAN,
        "upper_shadow": (today.high - hi_body) / base * 100 if base else NAN,
        "lower_shadow": (lo_body - today.low) / base * 100 if base else NAN,
        "volume_change": _change(today.volume, prev_bar.volume if prev_bar else None),
        "volume_ratio_5": _ratio(today.volume, [b.volume for b in prev[-5:]]),
        "volume_ratio_10": _ratio(today.volume, [b.volume for b in prev[-10:]]),
        "amount_change": _change(today.amount, prev_bar.amount if prev_bar else None),
        "amount_ratio_5": _ratio(today.amount, [b.amount for b in prev[-5:]]),
        "amount_ratio_10": _ratio(today.amount, [b.amount for b in prev[-10:]]),
        "turnover_rate": NAN if today.turnover_rate is None else float(today.turnover_rate),
        "turnover_change": (
            float(today.turnover_rate) - float(prev_bar.turnover_rate)
            if today.turnover_rate is not None and prev_bar is not None and prev_bar.turnover_rate is not None
            else NAN
        ),
        "position_5": _position(history, 5),
        "position_10": _position(history, 10),
        "position_20": _position(history, 20),
        "position_60": _position(history, 60),
    }
    # 给综合评分用：K 线实体饱满度 0~100（实体占振幅）
    span = today.high - today.low
    feats["body_percent"] = abs(today.close - today.open) / span * 100 if span > 0 else 0.0
    return feats


def _trend(values: list[float | None]) -> float:
    """趋势编码：三天依次走高 +1，依次走低 −1，其余 0；缺数 NaN。"""
    if any(value is None for value in values):
        return NAN
    a, b, c = values
    if a < b < c:
        return 1.0
    if a > b > c:
        return -1.0
    return 0.0


def _aggregates(days: list[dict]) -> list[float]:
    growth = 1.0
    for row in days:
        change = row["close_change"]
        growth = NAN if math.isnan(change) else growth * (1.0 + change / 100.0)
    vols = [row["volume"] for row in days]
    amts = [row["amount"] for row in days]
    turns = [row["turnover_rate_raw"] for row in days]
    return [
        (growth - 1.0) * 100.0,
        _change(vols[-1], vols[0]),
        _change(amts[-1], amts[0]),
        float(turns[-1]) - float(turns[0]) if turns[-1] is not None and turns[0] is not None else NAN,
        _trend([row["close"] for row in days]),
        _trend(vols),
        _trend(amts),
    ]


def _prepare(bars: list[Bar], as_of: date, include_as_of_date: bool) -> list[Bar]:
    """晚于评估日的行情丢掉；同一天留最后一根（上午半天线盖住库里的全天线）。"""
    kept: dict[date, Bar] = {}
    for bar in bars:
        if bar.trade_date > as_of or (bar.trade_date == as_of and not include_as_of_date):
            continue
        kept[bar.trade_date] = bar
    return [kept[key] for key in sorted(kept)]


def _valid(bar: Bar) -> bool:
    return all(v is not None and v > 0 for v in (bar.open, bar.high, bar.low, bar.close)) and bool(bar.volume and bar.volume > 0)


def build_three_day_window(
    bars: list[Bar],
    as_of: date,
    *,
    include_as_of_date: bool,
    expected_days: list[date] | None = None,
    listed_days: int | None = None,
) -> WindowResult:
    """三道闸：可用交易日 < 3 → insufficient_trading_days；完整交易日 < 20 → listed_less_than_20_days
    （半天线不占名额；listed_days 给了就用它，例如按上市日期数出来的交易日数）；
    窗口三天有缺行情/停牌/零成交 → invalid_bar_in_window（expected_days 给了还要求三天正好是这三个交易日）。"""
    eligible = _prepare(bars, as_of, include_as_of_date)
    if len(eligible) < 3:
        return WindowResult(False, "insufficient_trading_days")
    full = listed_days if listed_days is not None else sum(1 for bar in eligible if not bar.is_partial)
    if full < MIN_LISTED_TRADING_DAYS:
        return WindowResult(False, "listed_less_than_20_days")
    window = eligible[-3:]
    if expected_days is not None and [bar.trade_date for bar in window] != list(expected_days):
        return WindowResult(False, "invalid_bar_in_window", window)
    if not all(_valid(bar) for bar in window):
        return WindowResult(False, "invalid_bar_in_window", window)
    day_features = []
    for bar in window:
        history = [item for item in eligible if item.trade_date <= bar.trade_date]
        day_features.append(compute_day_features(history))
    vector: list[float] = []
    for feats in day_features:
        vector.extend(float(feats[name]) for name, _g, _i in DAILY_SPEC)
    vector.extend(_aggregates(day_features))
    if len(vector) != VECTOR_DIM:
        raise RuntimeError(f"向量维度是 {len(vector)}，应该是 {VECTOR_DIM}")
    return WindowResult(True, "", window, day_features, vector)


def raw_day(features: dict) -> dict:
    close, pre_close = features["close"], features["pre_close"]
    change = None if close is None or not pre_close else round((close / pre_close - 1.0) * 100, 4)
    return {
        "trade_date": features["trade_date"],
        "change_percent": change,
        "open": features["open"],
        "high": features["high"],
        "low": features["low"],
        "close": close,
        "pre_close": pre_close,
        "volume": features["volume"],
        "amount": features["amount"] or None,
        "turnover_rate": features["turnover_rate_raw"],
    }
