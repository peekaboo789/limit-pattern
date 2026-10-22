"""67 维形态向量：20 项日特征 × 3 天 + 7 项三日聚合。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from backend.app.core.constants import GROUP_WEIGHTS, MIN_LISTED_TRADING_DAYS, PRICE_INNER

# 顺序即向量里的顺序。前 60 维是 T-3、T-2、T-1 各一遍，后 7 维是三日聚合。
DAILY_SPEC: list[tuple[str, str]] = [
    ("open_change", "price"),
    ("high_change", "price"),
    ("low_change", "price"),
    ("close_change", "price"),
    ("body_percent", "kline"),
    ("upper_shadow", "kline"),
    ("lower_shadow", "kline"),
    ("amplitude", "kline"),
    ("volume_log", "volume"),
    ("volume_ratio_5", "volume"),
    ("volume_ratio_10", "volume"),
    ("amount_log", "amount"),
    ("amount_ratio_5", "amount"),
    ("amount_change", "amount"),
    ("turnover_rate", "turnover"),
    ("turnover_ratio_5", "turnover"),
    ("position_5", "position"),
    ("position_10", "position"),
    ("position_20", "position"),
    ("position_60", "position"),
]
AGGREGATE_NAMES = [
    "return_3d",
    "slope_close",
    "volatility_3d",
    "volume_trend",
    "amplitude_sum",
    "up_days",
    "position_change",
]
VECTOR_DIM = len(DAILY_SPEC) * 3 + len(AGGREGATE_NAMES)


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
        for name, _group in DAILY_SPEC:
            names.append(f"{offset}_{name}")
    names.extend(AGGREGATE_NAMES)
    return names


def dimension_weights(group_weights: dict[str, float] | None = None) -> list[float]:
    """每一维的权重，加总为 1。价格组内部按 close > high > low > open。"""
    groups = dict(GROUP_WEIGHTS if group_weights is None else group_weights)
    per_group_count: dict[str, int] = {}
    for _name, group in DAILY_SPEC:
        per_group_count[group] = per_group_count.get(group, 0) + 1
    weights: list[float] = []
    for _name, group in DAILY_SPEC:
        inner = PRICE_INNER[_name.split("_")[0]] if group == "price" else 1.0 / per_group_count[group]
        # 三个交易日平分该组权重
        weights.append(groups[group] / 3.0 * inner)
    weights = weights * 3
    trend = groups["trend"] / len(AGGREGATE_NAMES)
    weights.extend([trend] * len(AGGREGATE_NAMES))
    return weights


def _pct(price: float | None, base: float | None) -> float:
    if price is None or base is None or base <= 0:
        return 0.0
    return (price / base - 1.0) * 100.0


def _position(history: list[Bar], window: int) -> float:
    chunk = history[-window:]
    if not chunk:
        return 0.5
    lo = min(bar.low for bar in chunk)
    hi = max(bar.high for bar in chunk)
    if hi <= lo:
        return 0.5
    return (chunk[-1].close - lo) / (hi - lo)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _ratio(current: float | None, previous: list[float]) -> float:
    baseline = _mean([value for value in previous if value > 0])
    if current is None or baseline is None or baseline <= 0:
        return 1.0
    return current / baseline


def compute_day_features(history: list[Bar]) -> dict:
    """history 最后一根是当天，更早的都在它前面。天数不够就按已有的天数算，列仍然有值。"""
    today = history[-1]
    base = today.pre_close if today.pre_close and today.pre_close > 0 else (
        history[-2].close if len(history) >= 2 else today.close
    )
    previous = history[:-1]
    volumes = [bar.volume or 0.0 for bar in previous]
    amounts = [bar.amount for bar in previous if bar.amount is not None]
    turnovers = [bar.turnover_rate for bar in previous if bar.turnover_rate is not None]
    amount = today.amount
    turnover = today.turnover_rate
    prev_amount = amounts[-1] if amounts else None
    features = {
        "trade_date": today.trade_date.isoformat(),
        "open": today.open,
        "high": today.high,
        "low": today.low,
        "close": today.close,
        "pre_close": base,
        "volume": today.volume,
        "amount": amount,
        "turnover_rate": turnover,
        "is_partial": today.is_partial,
        "open_change": _pct(today.open, base),
        "high_change": _pct(today.high, base),
        "low_change": _pct(today.low, base),
        "close_change": _pct(today.close, base),
        "body_percent": _pct(today.close, base) - _pct(today.open, base),
        "upper_shadow": _pct(today.high, base) - max(_pct(today.open, base), _pct(today.close, base)),
        "lower_shadow": min(_pct(today.open, base), _pct(today.close, base)) - _pct(today.low, base),
        "amplitude": _pct(today.high, base) - _pct(today.low, base),
        "volume_log": math.log1p(today.volume or 0.0),
        "volume_ratio_5": _ratio(today.volume, volumes[-5:]),
        "volume_ratio_10": _ratio(today.volume, volumes[-10:]),
        "amount_log": math.log1p(amount) if amount is not None else 0.0,
        "amount_ratio_5": _ratio(amount, [value for value in amounts[-5:]]) if amount is not None else 0.0,
        "amount_change": ((amount - prev_amount) / prev_amount) if amount is not None and prev_amount else 0.0,
        "turnover_rate_feature": turnover if turnover is not None else 0.0,
        "turnover_ratio_5": _ratio(turnover, [value for value in turnovers[-5:]]) if turnover is not None else 0.0,
        "position_5": _position(history, 5),
        "position_10": _position(history, 10),
        "position_20": _position(history, 20),
        "position_60": _position(history, 60),
    }
    # 向量里换手率用补过 0 的那一列，库里的原值仍可以是 null
    features["turnover_rate_model"] = features["turnover_rate_feature"]
    return features


def _day_vector(features: dict) -> list[float]:
    values: list[float] = []
    for name, _group in DAILY_SPEC:
        if name == "turnover_rate":
            values.append(float(features["turnover_rate_model"]))
        else:
            values.append(float(features[name]))
    return values


def _aggregates(day_features: list[dict]) -> list[float]:
    closes = [row["close_change"] for row in day_features]
    # 三日收益，单位是百分数
    growth = 1.0
    for change in closes:
        growth *= 1.0 + change / 100.0
    return_3d = (growth - 1.0) * 100.0
    slope = closes[-1] - closes[0]
    mean = sum(closes) / len(closes)
    variance = sum((item - mean) ** 2 for item in closes) / len(closes)
    volumes = [row["volume"] or 0.0 for row in day_features]
    baseline = (volumes[0] + volumes[1]) / 2.0
    volume_trend = volumes[2] / baseline if baseline > 0 else 1.0
    amplitude_sum = sum(row["amplitude"] for row in day_features)
    up_days = float(sum(1 for change in closes if change > 0))
    position_change = day_features[-1]["position_20"] - day_features[0]["position_20"]
    return [return_3d, slope, math.sqrt(variance), volume_trend, amplitude_sum, up_days, position_change]


def _prepare(bars: list[Bar], as_of: date, include_as_of_date: bool) -> list[Bar]:
    """晚于评估日的行情先丢掉。同一天留最后一根，这样上午半天线能盖住库里的全天线。"""
    kept: dict[date, Bar] = {}
    for bar in bars:
        if bar.trade_date > as_of:
            continue
        if not include_as_of_date and bar.trade_date == as_of:
            continue
        kept[bar.trade_date] = bar
    ordered = [kept[key] for key in sorted(kept)]
    # 前收缺失时用上一根收盘价补上，避免窗口第一天没有涨跌幅
    previous: float | None = None
    prepared: list[Bar] = []
    for bar in ordered:
        pre_close = bar.pre_close if bar.pre_close and bar.pre_close > 0 else previous
        prepared.append(
            Bar(
                trade_date=bar.trade_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                pre_close=pre_close,
                volume=bar.volume,
                amount=bar.amount,
                turnover_rate=bar.turnover_rate,
                is_partial=bar.is_partial,
            )
        )
        previous = bar.close
    return prepared


def build_three_day_window(
    bars: list[Bar],
    as_of: date,
    *,
    include_as_of_date: bool,
) -> WindowResult:
    """样本窗口严格早于涨停日；候选窗口含评估日。两边故意错开一天。"""
    eligible = _prepare(bars, as_of, include_as_of_date)
    if len(eligible) < MIN_LISTED_TRADING_DAYS:
        return WindowResult(False, "insufficient_window")
    window = eligible[-3:]
    day_features: list[dict] = []
    for bar in window:
        history = [item for item in eligible if item.trade_date <= bar.trade_date]
        day_features.append(compute_day_features(history))
    vector: list[float] = []
    for features in day_features:
        vector.extend(_day_vector(features))
    vector.extend(_aggregates(day_features))
    if len(vector) != VECTOR_DIM:
        raise RuntimeError(f"向量维度是 {len(vector)}，应该是 {VECTOR_DIM}")
    return WindowResult(True, "", window, day_features, vector)


def raw_day(features: dict) -> dict:
    """相似匹配页要的原始数值。成交额、换手率缺了就保持 null，页面上写「—」。"""
    close = features["close"]
    pre_close = features["pre_close"]
    change = None
    if close is not None and pre_close:
        change = close / pre_close - 1.0
    return {
        "trade_date": features["trade_date"],
        "change_percent": None if change is None else round(change * 100, 4),
        "open": features["open"],
        "high": features["high"],
        "low": features["low"],
        "close": close,
        "pre_close": pre_close,
        "volume": features["volume"],
        "amount": features["amount"] if features["amount"] else None,
        "turnover_rate": features["turnover_rate"] if features["turnover_rate"] else None,
    }
