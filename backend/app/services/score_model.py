"""综合评分、三个分项、单股历史总分、预测分。逐条照旧版《AI打分公式》实现，分数全部由本系统算，模型只是解读器。

composite = round(clip(Σ wᵢ·指标ᵢ / Σ(有项的 wᵢ) − 风险扣分, 0, 100), 2)
"""

from __future__ import annotations

from backend.app.core.constants import (
    MIN_BAND_OBSERVATIONS,
    PREDICTION_LIFT_CAP,
    PREDICTION_WEIGHT_CARRY,
    PREDICTION_WEIGHT_LIFT,
    PREDICTION_WEIGHT_SHAPE,
)

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
ITEM_LABELS = {"pattern": "形态", "technical": "技术", "volume": "量能", "capital": "资金", "history": "历史成功率"}
INDICATOR_OF_ITEM = {key: field for key, field, _weight in ITEMS}
INDICATOR_ORDER = (
    "similarity_score",
    "pattern_match_rate",
    "technical_score",
    "volume_score",
    "capital_score",
    "history_limit_up_rate",
    "risk_score",
)
INDICATOR_LABELS = {
    "similarity_score": "三日形态相似度",
    "pattern_match_rate": "近期涨停形态匹配率",
    "technical_score": "技术形态评分",
    "volume_score": "成交量评分",
    "capital_score": "资金评分",
    "history_limit_up_rate": "历史相似样本成功率",
    "risk_score": "风险评分",
}

# 下界含、上界不含
RISK_TIERS: tuple[tuple[float, float], ...] = ((80.0, 20.0), (60.0, 10.0), (40.0, 5.0))
LEVEL_TIERS: tuple[tuple[float, str], ...] = ((90.0, "强关注"), (80.0, "高关注"), (70.0, "观察"), (60.0, "一般"))
LOW_LEVEL = "低匹配"

FEATURE_COLUMNS = ("position_10", "position_20", "position_60", "body_percent", "close_change", "volume_ratio_5", "volume_ratio_10", "amplitude")


def _whole(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _pct(weight: float) -> str:
    return f"{weight * 100:g}%"


FORMULA_TEXT = (
    "综合评分 = Σ(权重×指标) / Σ(有数的权重) − 风险扣分，夹在 0–100，保留两位小数。权重："
    + " / ".join(f"{ITEM_LABELS[k]} {_pct(w)}" for k, _f, w in ITEMS)
    + "。缺了的项按剩余权重摊开，不把没数据当成 0 分。风险扣分："
    + "，".join(f"风险分 ≥{_whole(f)} 扣 {_whole(p)}" for f, p in RISK_TIERS)
    + "，不到 40 不扣，风险分缺数不扣。等级："
    + "，".join(f"≥{_whole(f)} {n}" for f, n in LEVEL_TIERS)
    + f"，其余 {LOW_LEVEL}。近期涨停形态匹配率只展示，不参与加权。"
)

ROLE_PREAMBLE = (
    "你是一名资深 A 股短线交易与涨停板研究员，多年复盘涨停板、连板、首板、炸板、分歧转一致、龙头股和情绪周期。"
    "评分模型由本系统给定，你不是打分器而是解读器。"
)

PROMPT_RUBRIC = (
    ROLE_PREAMBLE
    + FORMULA_TEXT
    + "每只候选的综合评分、等级、各项贡献你已经有了，不要重新计算，也不要修改输入数据。"
    + "你写的 score 是你自己的相对判断，页面以本系统的综合评分为准，并把两者的差一起显示；"
    + "reason 要写成模型依据：在给定权重下你更信哪几项、哪一项的读数让你要偏离这个等级，不要复述字段清单。"
    + "capital_score 现在是 null：null 不是 0 分，是这一项无从谈起。pattern_match_rate 不在加权项里，可以拿它说事，但不要拿它改分。"
    + "不加入新闻、公告、基本面、消息，不预测确定涨停，不写买入建议或目标价。"
)

NULL_RULE = "null 不是 0 分，也不是很低，它是这一项无从谈起。"
CAPITAL_RULE = "资金分没有真实资金流数据，恒为 null，不写 0。"
HISTORY_RULE = "历史成功率是相似涨停样本次日继续涨停的已发生频率，不是对这一只股票的预测。"
PUBLIC_TEXTS = (ROLE_PREAMBLE, FORMULA_TEXT, PROMPT_RUBRIC, NULL_RULE, CAPITAL_RULE, HISTORY_RULE)


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _clamped(value) -> float | None:
    return None if value is None else _clip(float(value))


def risk_penalty(risk_score: float | None) -> float:
    if risk_score is None:
        return 0.0
    for floor, points in RISK_TIERS:
        if risk_score >= floor:
            return points
    return 0.0


risk_penalty_of = risk_penalty


def level_of(score: float | None) -> str:
    if score is None:
        return ""
    for floor, name in LEVEL_TIERS:
        if score >= floor:
            return name
    return LOW_LEVEL


def score_of(indicators: dict, model_score: float | None = None) -> dict:
    """五项加权。返回的账里已经带 has_signal（weight_total > 0）。"""
    present: list[tuple[str, float, float]] = []
    missing: list[str] = []
    for key, field, weight in ITEMS:
        value = _clamped(indicators.get(field))
        if value is None:
            missing.append(field)
        else:
            present.append((key, weight, value))
    weight_total = sum(weight for _k, weight, _v in present)
    contributions: dict[str, float | None] = {key: None for key, _f, _w in ITEMS}
    has_signal = weight_total > 0
    weighted = sum(w * v for _k, w, v in present) / weight_total if has_signal else 0.0
    for key, weight, value in present:
        contributions[key] = round(weight * value / weight_total, 2)
    risk = indicators.get("risk_score")
    if risk is None:
        missing.append("risk_score")
    penalty = risk_penalty(None if risk is None else float(risk))
    composite = round(_clip(weighted - penalty), 2) if has_signal else 0.0
    gap = None if (model_score is None or not has_signal) else round(float(model_score) - composite, 2)
    return {
        "composite_score": composite,
        "weighted_score": round(weighted, 2),
        "level": level_of(composite) if has_signal else "",
        "contributions": contributions,
        "risk_penalty": penalty,
        "missing_items": missing,
        "weight_total": round(weight_total, 4),
        "indicators": {k: indicators.get(k) for k in INDICATOR_ORDER},
        "model_score": model_score,
        "model_gap": gap,
        "has_signal": has_signal,
    }


def has_signal(formula: dict | None) -> bool:
    return isinstance(formula, dict) and float(formula.get("weight_total") or 0) > 0 and formula.get("has_signal") is not False


def read_composite(formula: dict | None) -> float | None:
    if not isinstance(formula, dict) or "has_signal" not in formula or not formula.get("has_signal"):
        return None
    value = formula.get("composite_score")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def read_level(formula: dict | None) -> str:
    return str(formula.get("level") or "") if read_composite(formula) is not None else ""


def read_gap(formula: dict | None) -> float | None:
    if read_composite(formula) is None:
        return None
    gap = formula.get("model_gap")
    return float(gap) if isinstance(gap, (int, float)) and not isinstance(gap, bool) else None


def with_gap(formula: dict, model_score: float | None) -> dict:
    merged = dict(formula)
    composite = read_composite(merged)
    merged["model_score"] = model_score
    merged["model_gap"] = None if composite is None or model_score is None else round(float(model_score) - composite, 2)
    return merged


def _blend(parts: list[tuple[float, float | None]]) -> float | None:
    usable = [(w, v) for w, v in parts if v is not None]
    total = sum(w for w, _v in usable)
    if total <= 0:
        return None
    return round(sum(w * v for w, v in usable) / total, 2)


def _volume_points(x: float) -> float:
    x = float(x)
    if x < 0:
        return 0.0
    if x < 1:
        return 30.0 + 30.0 * x
    if x < 2:
        return 60.0 + 25.0 * (x - 1.0)
    if x < 3:
        return 85.0 + 10.0 * (x - 2.0)
    return 95.0


def technical_score_of(features: dict | None) -> float | None:
    if not features:
        return None
    positions = [float(features[k]) for k in ("position_10", "position_20", "position_60") if features.get(k) is not None]
    position = _clamped(sum(positions) / len(positions) * 100.0) if positions else None
    body = _clamped(features.get("body_percent"))
    change = features.get("close_change")
    momentum = None if change is None else _clip(50.0 + float(change) * 10.0)
    return _blend([(0.45, position), (0.30, body), (0.25, momentum)])


def volume_score_of(features: dict | None) -> float | None:
    if not features:
        return None
    five = features.get("volume_ratio_5")
    ten = features.get("volume_ratio_10")
    return _blend([(0.60, None if five is None else _volume_points(five)), (0.40, None if ten is None else _volume_points(ten))])


def risk_score_of(features: dict | None, change_percent: float | None = None) -> float | None:
    """越高越危险。追高用实时涨幅；跌和平盘记 0（不是缺项），只有 change_percent 为 None 才缺。"""
    features = features or {}
    p20 = features.get("position_20")
    amp = features.get("amplitude")
    ratio = features.get("volume_ratio_5")
    return _blend(
        [
            (0.35, None if p20 is None else _clip(float(p20) * 100.0)),
            (0.25, None if change_percent is None else _clip(max(0.0, float(change_percent)) * 10.0)),
            (0.20, None if amp is None else _clip(float(amp) * 5.0)),
            (0.20, None if ratio is None else _clip(max(0.0, float(ratio) - 3.0) * 25.0)),
        ]
    )


def capital_score_of(_features: dict | None) -> float | None:
    """没有真实资金流（主力/大单净额）数据：恒为 None，不用成交额/换手冒充。"""
    return None


def pattern_match_rate(high_count: int | None, sample_count: int | None) -> float | None:
    if high_count is None or sample_count is None or sample_count <= 0:
        return None
    return round(high_count / sample_count * 100.0, 2)


def build_indicators(*, similarity, high_count, sample_count, features, change_percent, history_rate_percent) -> dict:
    return {
        "similarity_score": None if similarity is None else round(float(similarity), 2),
        "pattern_match_rate": pattern_match_rate(high_count, sample_count),
        "technical_score": technical_score_of(features),
        "volume_score": volume_score_of(features),
        "capital_score": capital_score_of(features),
        "history_limit_up_rate": None if history_rate_percent is None else round(float(history_rate_percent), 2),
        "risk_score": risk_score_of(features, change_percent),
    }


# ---------------- 单股历史总分（ai_pick_stats） ----------------

HISTORY_FORMULA = "avg_score × (0.3 + 0.7 × (h+1)/(settled+2))"
ALL_PENDING_FORMULA = "avg_score（samples all pending — no hit/miss yet）"


def score_from_history(avg_score: float | None, hit: int, settled: int) -> int | None:
    if avg_score is None:
        return None
    if settled <= 0:
        return max(0, min(100, int(round(float(avg_score)))))
    raw = float(avg_score) * (0.3 + 0.7 * (hit + 1) / (settled + 2))
    return max(0, min(100, int(round(raw))))


def hit_rate(hit: int, miss: int) -> float | None:
    return None if hit + miss == 0 else round(hit * 100 / (hit + miss), 2)


# ---------------- 预测分（prediction_service） ----------------

EVIDENCE_MATCH_LIMIT = 20


def weighted_carry_rate(evidence: list[tuple[float, bool]]) -> float | None:
    """相似度加权连板率：Σ(相似度·是否次日涨停) / Σ相似度 × 100。"""
    evidence = evidence[:EVIDENCE_MATCH_LIMIT]
    weight = sum(max(0.0, float(s)) for s, _hit in evidence)
    if weight <= 0:
        return None
    hit_weight = sum(max(0.0, float(s)) for s, hit in evidence if hit)
    return round(hit_weight / weight * 100.0, 2)


def band_term(band_lift: float | None) -> float | None:
    if band_lift is None:
        return None
    return round(min(float(band_lift), PREDICTION_LIFT_CAP) / PREDICTION_LIFT_CAP * 100.0, 2)


def prediction_score(shape: float | None, carry: float | None, band_lift: float | None) -> dict:
    terms = {"shape": shape, "carry": carry, "band": band_term(band_lift)}
    score = _blend(
        [(PREDICTION_WEIGHT_SHAPE, shape), (PREDICTION_WEIGHT_CARRY, carry), (PREDICTION_WEIGHT_LIFT, terms["band"])]
    )
    return {"prediction_score": score, "terms": terms, "band_lift": band_lift, "min_band_observations": MIN_BAND_OBSERVATIONS}
