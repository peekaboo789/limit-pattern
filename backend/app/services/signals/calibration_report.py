"""校准报告：把 AI 置信度与命中标签对齐，产出可靠性曲线、Brier、ECE、分箱统计。

复用 algorithms.calibration 的 brier_score / expected_calibration_error。
"""

from __future__ import annotations

from backend.app.algorithms.calibration import brier_score, expected_calibration_error


def reliability_curve(
    probabilities: list[float],
    labels: list[int],
    bins: int = 10,
) -> dict:
    """按置信度分箱，每箱给出样本数、平均置信度、实际命中率、校准误差。"""
    if not probabilities:
        return {"bins": [], "n": 0}
    bucket_n = [0] * bins
    bucket_p = [0.0] * bins
    bucket_l = [0.0] * bins
    for prob, label in zip(probabilities, labels):
        index = min(bins - 1, int(prob * bins))
        bucket_n[index] += 1
        bucket_p[index] += prob
        bucket_l[index] += label
    out = []
    for index in range(bins):
        if bucket_n[index] == 0:
            out.append(
                {
                    "bin": index,
                    "lower": round(index / bins, 2),
                    "upper": round((index + 1) / bins, 2),
                    "n": 0,
                    "confidence": None,
                    "hit_rate": None,
                    "gap": None,
                }
            )
            continue
        confidence = bucket_p[index] / bucket_n[index]
        hit_rate = bucket_l[index] / bucket_n[index]
        out.append(
            {
                "bin": index,
                "lower": round(index / bins, 2),
                "upper": round((index + 1) / bins, 2),
                "n": bucket_n[index],
                "confidence": round(confidence, 4),
                "hit_rate": round(hit_rate, 4),
                "gap": round(confidence - hit_rate, 4),
            }
        )
    return {"bins": out, "n": len(probabilities)}


def report(probabilities: list[float], labels: list[int], bins: int = 10) -> dict:
    """完整校准报告。labels 取 0/1（1 = 命中）。空输入返回 None 值。"""
    return {
        "n": len(probabilities),
        "brier": None if not probabilities else round(brier_score(probabilities, labels), 4),
        "ece": None if not probabilities else round(expected_calibration_error(probabilities, labels, bins), 4),
        "mean_confidence": None if not probabilities else round(sum(probabilities) / len(probabilities), 4),
        "hit_rate": None if not probabilities else round(sum(labels) / len(labels), 4),
        "reliability": reliability_curve(probabilities, labels, bins),
    }
