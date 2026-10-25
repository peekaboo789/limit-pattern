"""一行的综合评分怎么算：只有这一处。

盘中/AI 那条链路（`ai_evidence`）和次日预测池（`prediction_service`）以前各算各的：
一边把「形态+次日连板+档位提升」三证据加权叫「综合分」，一边按 `score_model` 的五项加权
叫「综合评分」——同一只股票同一天在两个页面上顶着同一个名字，数却不一样。
现在两项都从这里的 `score_row()` 出来，`score_model.score_of` 是全app唯一的综合评分算式。

单位口径也钉在这一处：`band_rate_percent` 与 `ledger_rate_percent` **都必须是百分数（0-100）**。
档位发生率在回测侧是小数（0-1）、在实时榜那一行是百分数，两边直接混喂会把分数量出一半的差，
所以调用方负责换算，这个函数只认百分数。
"""

from __future__ import annotations

from datetime import date

from backend.app.algorithms.features import compute_day_features
from backend.app.core.constants import MIN_BAND_OBSERVATIONS
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.score_model import build_indicators, formula_for

HISTORY_FROM_BAND = "band_rate"
HISTORY_FROM_LEDGER = "ai_pick_ledger"


def features_at(session, codes: list[str], day: date | None) -> dict[str, dict]:
    """那一天那一根的日线特征。那一天没有行情（停牌、或盘中还没落库）就整只不给，
    不拿更早那根冒充——冒充出来的分是昨天的分，榜上写的价是今天的价。"""
    if day is None or not codes:
        return {}
    grouped = load_bars_bulk(session, day, codes=codes)
    found: dict[str, dict] = {}
    for code, bars in grouped.items():
        if not bars or bars[-1].trade_date != day:
            continue
        found[code] = compute_day_features(bars)
    return found


def gated_band_rate(band_rate_percent, band_samples) -> float | None:
    """档位发生率进综合评分前的那道门槛：观察数不够 MIN_BAND_OBSERVATIONS 就等于没数。

    门槛写在这一处，两个页面才不会一个卡 30 条、一个卡 20 条。
    """
    if band_rate_percent is None or int(band_samples or 0) < MIN_BAND_OBSERVATIONS:
        return None
    return float(band_rate_percent)


def score_row(
    *,
    similarity: float | None,
    high_count: int | None,
    sample_count: int | None,
    features: dict | None,
    band_rate_percent,
    band_samples,
    ledger_rate_percent=None,
) -> dict:
    """把一行折成 `score_model` 那套指标与综合评分。

    历史成功率那一项的出处先回测档位、后这只股票自己的 AI 推荐账，两个都不能混着给，
    所以用了哪个也一并回给调用方（`history_source`）。
    """
    band_rate = gated_band_rate(band_rate_percent, band_samples)
    history_rate = band_rate
    history_source = HISTORY_FROM_BAND if history_rate is not None else None
    if history_rate is None and ledger_rate_percent is not None:
        history_rate = float(ledger_rate_percent)
        history_source = HISTORY_FROM_LEDGER

    indicators = build_indicators(
        similarity=similarity,
        high_count=high_count,
        sample_count=sample_count,
        features=features,
        history_rate_percent=history_rate,
    )
    formula = formula_for(indicators)
    formula["history_source"] = history_source
    return {
        "indicators": indicators,
        "formula": formula,
        "history_rate": history_rate,
        "history_source": history_source,
        "band_next_day_rate_percent": band_rate,
        "band_next_day_samples": int(band_samples or 0) or None,
    }


__all__ = ["HISTORY_FROM_BAND", "HISTORY_FROM_LEDGER", "features_at", "gated_band_rate", "score_row"]
