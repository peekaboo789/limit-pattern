"""回测分档的概率校准：Brier、对数损失、期望校准误差。"""

from __future__ import annotations

import math


def brier_score(probabilities: list[float], labels: list[int]) -> float | None:
    if not probabilities:
        return None
    total = 0.0
    for prob, label in zip(probabilities, labels):
        total += (prob - label) ** 2
    return total / len(probabilities)


def log_loss(probabilities: list[float], labels: list[int], eps: float = 1e-12) -> float | None:
    if not probabilities:
        return None
    total = 0.0
    for prob, label in zip(probabilities, labels):
        clipped = min(1.0 - eps, max(eps, prob))
        total += -(label * math.log(clipped) + (1 - label) * math.log(1 - clipped))
    return total / len(probabilities)


def expected_calibration_error(
    probabilities: list[float],
    labels: list[int],
    bins: int = 10,
) -> float | None:
    if not probabilities:
        return None
    counts = [0] * bins
    prob_sum = [0.0] * bins
    label_sum = [0.0] * bins
    for prob, label in zip(probabilities, labels):
        index = min(bins - 1, int(prob * bins))
        counts[index] += 1
        prob_sum[index] += prob
        label_sum[index] += label
    total = len(probabilities)
    error = 0.0
    for index, count in enumerate(counts):
        if count == 0:
            continue
        gap = abs(prob_sum[index] / count - label_sum[index] / count)
        error += count / total * gap
    return error
