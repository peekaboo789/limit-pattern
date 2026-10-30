"""综合评分。分数由本系统按固定权重算，模型只写依据。"""

from __future__ import annotations

from backend.app.core.constants import MIN_BAND_OBSERVATIONS

# 权重固定。pattern_match_rate 不在这张表里。
WEIGHT_PATTERN = 0.35
WEIGHT_TECHNICAL = 0.20
WEIGHT_VOLUME = 0.15
WEIGHT_CAPITAL = 0.15
WEIGHT_HISTORY = 0.15

ITEMS: tuple[tuple[str, str, float], ...] = (
    ("pattern", "similarity_score", WEIGHT_PATTERN),
    ("technical", "technical_score", WEIGHT_TECHNICAL),
    ("volume", "volume_score", WEIGHT_VOLUME),
    ("capital", "capital_score", WEIGHT_CAPITAL),
    ("history", "history_limit_up_rate", WEIGHT_HISTORY),
)

INDICATOR_OF_ITEM = {key: field for key, field, _weight in ITEMS}

# 下界含、上界不含。80 扣 20，60 扣 10，40 扣 5。
RISK_TIERS: tuple[tuple[float, float], ...] = ((80, 20), (60, 10), (40, 5))
LEVEL_TIERS: tuple[tuple[float, str], ...] = (
    (90, "强关注"),
    (80, "高关注"),
    (70, "观察"),
    (60, "一般"),
)

FEATURE_COLUMNS = (
    "position_10",
    "position_20",
    "position_60",
    "body_percent",
    "close_change",
    "volume_ratio_5",
    "volume_ratio_10",
    "amplitude",
)


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _whole(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _risk_text() -> str:
    parts = []
    for floor, points in RISK_TIERS:
        parts.append(f"{_whole(floor)} 起扣 {_whole(points)} 分")
    return "，".join(parts)


def _level_text() -> str:
    chunks = [f"{_whole(floor)} 分起为{name}" for floor, name in LEVEL_TIERS]
    return "，".join(chunks) + "，不到 60 分为低匹配"


FORMULA_TEXT = (
    "综合评分按固定权重计算："
    f"形态 {_fmt(WEIGHT_PATTERN)}、技术 {_fmt(WEIGHT_TECHNICAL)}、"
    f"量能 {_fmt(WEIGHT_VOLUME)}、资金 {_fmt(WEIGHT_CAPITAL)}、"
    f"历史成功率 {_fmt(WEIGHT_HISTORY)}。"
    "缺了的项按剩余权重摊开，不把没数据当成 0 分。"
    f"风险扣分：{_risk_text()}。risk_score 没有数时不扣分。"
    f"等级：{_level_text()}。"
    "近五日形态匹配率只展示，不参与加权。"
)

PROMPT_RUBRIC = (
    FORMULA_TEXT
    + "这些数你已经有了，不要重新计算，也不要修改输入。"
    + "null 不是 0 分，它是这一项无从谈起。"
    + "请按公式分给出等级，并在依据里说明你和公式分差在哪里。"
    + "不加入新闻、公告、基本面、消息，也不预测确定涨停。"
)

ROLE_PREAMBLE = (
    "你是一名资深 A 股短线交易与涨停板研究员，多年复盘涨停板、连板、首板、炸板、分歧转一致、龙头股和情绪周期。你的任务：根据已经计算好的量化指标进行综合评分。"
    "重要规则：1. 不重新计算指标。2. 不修改输入数据。3. 不加入新闻、公告、基本面、消息。"
    "4. 不预测确定涨停。5. 输出的是基于历史模型指标的综合评分。"
)

NULL_RULE = "null 不是 0 分，也不是很低，它是这一项无从谈起。"
CAPITAL_RULE = "资金分没有成交额原料时留空，不写 0。"
HISTORY_RULE = "历史成功率取该档已经发生过的频率，不是模型对这一只股票的预测。"

PUBLIC_TEXTS = (ROLE_PREAMBLE, FORMULA_TEXT, PROMPT_RUBRIC, NULL_RULE, CAPITAL_RULE, HISTORY_RULE)


def risk_penalty(risk_score: float | None) -> float:
    if risk_score is None:
        return 0.0
    for floor, points in RISK_TIERS:
        if risk_score >= floor:
            return float(points)
    return 0.0


def level_of(score: float) -> str:
    for floor, name in LEVEL_TIERS:
        if score >= floor:
            return name
    return "低匹配"


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def score_of(indicators: dict) -> dict:
    """五项加权。全缺时 composite_score 写成 0，但 has_signal 为假，读的人必须把它当成没有分。"""
    present: list[tuple[str, float, float]] = []
    missing: list[str] = []
    for key, field, weight in ITEMS:
        value = indicators.get(field)
        if value is None:
            missing.append(field)
            continue
        present.append((key, weight, _clip(float(value))))
    weight_total = sum(weight for _key, weight, _value in present)
    contributions: dict[str, float | None] = {key: None for key, _field, _weight in ITEMS}
    if weight_total <= 0:
        weighted = 0.0
        has_signal = False
    else:
        has_signal = True
        weighted = sum(weight * value for _key, weight, value in present) / weight_total
        for key, weight, value in present:
            contributions[key] = round(weight / weight_total * value, 2)
    if indicators.get("risk_score") is None:
        missing.append("risk_score")
    penalty = risk_penalty(None if indicators.get("risk_score") is None else float(indicators["risk_score"]))
    composite = _clip(weighted - penalty) if has_signal else 0.0
    return {
        "composite_score": round(composite, 2),
        "weighted": round(weighted, 2),
        "risk_penalty": penalty,
        "level": level_of(composite) if has_signal else "",
        "contributions": contributions,
        "missing_items": missing,
        "weight_total": round(weight_total, 4),
        "has_signal": has_signal,
    }


def read_composite(formula: dict | None) -> float | None:
    if not isinstance(formula, dict) or not formula.get("has_signal"):
        return None
    value = formula.get("composite_score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def read_level(formula: dict | None) -> str:
    if read_composite(formula) is None:
        return ""
    assert formula is not None
    return str(formula.get("level") or "")


def read_gap(formula: dict | None) -> float | None:
    if read_composite(formula) is None or not isinstance(formula, dict):
        return None
    gap = formula.get("model_gap")
    if isinstance(gap, bool) or not isinstance(gap, (int, float)):
        return None
    return float(gap)


def with_gap(formula: dict, model_score: float | None) -> dict:
    merged = dict(formula)
    composite = read_composite(merged)
    if composite is None or model_score is None:
        merged["model_gap"] = None
    else:
        merged["model_gap"] = round(float(model_score) - composite, 2)
    return merged


def _volume_points(ratio: float) -> float:
    ratio = max(0.0, float(ratio))
    if ratio >= 3:
        return 95.0
    knots = ((0.0, 30.0), (1.0, 60.0), (2.0, 85.0), (3.0, 95.0))
    for (left, left_score), (right, right_score) in zip(knots, knots[1:]):
        if left <= ratio <= right:
            span = right - left
            return left_score + (ratio - left) / span * (right_score - left_score)
    return 30.0


def _blend(parts: list[tuple[float, float]]) -> float | None:
    usable = [(weight, value) for weight, value in parts if value is not None]
    total = sum(weight for weight, _value in usable)
    if total <= 0:
        return None
    return round(sum(weight / total * value for weight, value in usable), 2)


def technical_from_features(features: dict | None) -> float | None:
    if not features:
        return None
    positions = []
    for key in ("position_10", "position_20", "position_60"):
        if features.get(key) is None:
            continue
        positions.append(_clip(float(features[key]) * 100.0))
    position = sum(positions) / len(positions) if positions else None
    body = None if features.get("body_percent") is None else _clip(50.0 + float(features["body_percent"]) * 10.0)
    momentum = None if features.get("close_change") is None else _clip(50.0 + float(features["close_change"]) * 10.0)
    return _blend([(0.45, position), (0.30, body), (0.25, momentum)])


def volume_from_features(features: dict | None) -> float | None:
    if not features:
        return None
    five = None if features.get("volume_ratio_5") is None else _volume_points(features["volume_ratio_5"])
    ten = None if features.get("volume_ratio_10") is None else _volume_points(features["volume_ratio_10"])
    return _blend([(0.60, five), (0.40, ten)])


def risk_from_features(features: dict | None) -> float | None:
    if not features:
        return None
    position = None if features.get("position_20") is None else _clip(float(features["position_20"]) * 100.0)
    change = features.get("close_change")
    chase = None if change is None else _clip(max(float(change), 0.0) * 10.0)
    amplitude = None if features.get("amplitude") is None else _clip(float(features["amplitude"]) * 5.0)
    ratio = features.get("volume_ratio_5")
    burst = None if ratio is None else _clip(max(float(ratio) - 3.0, 0.0) * 25.0)
    return _blend([(0.35, position), (0.25, chase), (0.20, amplitude), (0.20, burst)])


def capital_from_features(_features: dict | None) -> float | None:
    """默认源没有成交额。没有原料就写 null，不写 0。"""
    return None


def pattern_match_rate(high_count: int | None, sample_count: int | None) -> float | None:
    if high_count is None or sample_count is None or sample_count <= 0:
        return None
    return round(high_count / sample_count * 100.0, 4)


def build_indicators(
    *,
    similarity: float | None,
    high_count: int | None,
    sample_count: int | None,
    features: dict | None,
    history_rate_percent: float | None,
) -> dict:
    return {
        "similarity_score": None if similarity is None else round(float(similarity), 4),
        "pattern_match_rate": pattern_match_rate(high_count, sample_count),
        "technical_score": technical_from_features(features),
        "volume_score": volume_from_features(features),
        "capital_score": capital_from_features(features),
        "history_limit_up_rate": None if history_rate_percent is None else round(float(history_rate_percent), 4),
        "risk_score": risk_from_features(features),
    }


def formula_for(indicators: dict) -> dict:
    formula = score_of(indicators)
    formula["min_band_observations"] = MIN_BAND_OBSERVATIONS
    return formula
