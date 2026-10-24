"""回测分档的概率校准：Brier、对数损失、期望校准误差、校准曲线。

这一份只吃数组、不碰库：系统给出的统计概率是「这一档历史上次日涨停的比例」，
指标回答的是「这个比例拿到没跑过的后半段上还准不准」。
读它的那一位（`backtest_service._calibrate`）负责决定谁是预测、谁是结果。

口径上有三件事是刻意的，改之前先看完：

* **空输入回 None，不回 0**。Brier 是「越小越好」，把「没样本」写成 0.0 等于宣布
  这套概率完美校准。
* **等宽分箱只输出非空段**。一屏全是 0 的箱子不说明任何事，只会把图挤满。
* **最后一箱右闭**。分档概率取的是 `hits/len(members)`，`p=1.0` 是合法值，
  按左闭右开写它会被整批漏掉——漏掉的还是最自信那一批。
"""

from __future__ import annotations

import math

#: 对数损失的概率裁剪区间：避免 log(0)，同时不让极端预测把指标拉爆
_EPS = 1e-6


def brier_score(probabilities: list[float], labels: list[int]) -> float | None:
    """Brier = mean((p − y)²)，越小越好；0 表示每一只都判对。"""
    if not probabilities:
        return None
    if len(probabilities) != len(labels):
        raise ValueError(f"预测与结果长度不一致：{len(probabilities)} vs {len(labels)}")
    total = 0.0
    for prob, label in zip(probabilities, labels):
        total += (float(prob) - float(label)) ** 2
    return round(total / len(probabilities), 6)


def log_loss(probabilities: list[float], labels: list[int], eps: float = _EPS) -> float | None:
    """对数损失 = −mean(y·log p + (1−y)·log(1−p))，越小越好。"""
    if not probabilities:
        return None
    if len(probabilities) != len(labels):
        raise ValueError(f"预测与结果长度不一致：{len(probabilities)} vs {len(labels)}")
    total = 0.0
    for prob, label in zip(probabilities, labels):
        clipped = min(1.0 - eps, max(eps, float(prob)))
        outcome = float(label)
        total += -(outcome * math.log(clipped) + (1.0 - outcome) * math.log(1.0 - clipped))
    return round(total / len(probabilities), 6)


def calibration_curve(probabilities: list[float], labels: list[int], bins: int = 10) -> list[dict]:
    """把 [0,1] 等宽分成 `bins` 段，只输出非空段。

    完美校准时每一段的 `mean_predicted` 与 `observed_rate` 相等。回测的预测值只有
    「这一档的历史发生率」那几个离散取值，所以段常常只落下一两个点——页面因此
    另外给一份按档位口径的对比（`band_checks`），那才是看得出错在哪的那张表。
    """
    if not probabilities:
        return []
    if len(probabilities) != len(labels):
        raise ValueError(f"预测与结果长度不一致：{len(probabilities)} vs {len(labels)}")
    if bins < 1:
        raise ValueError("分箱数必须 >= 1")
    points: list[dict] = []
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        # 最后一箱右闭：p=1.0 是合法预测，漏掉的是最自信那一批
        inside = [
            (prob, label)
            for prob, label in zip(probabilities, labels)
            if (lower <= prob < upper) or (index == bins - 1 and prob == upper)
        ]
        if not inside:
            continue
        count = len(inside)
        points.append(
            {
                "lower": round(lower, 4),
                "upper": round(upper, 4),
                "mean_predicted": round(sum(float(prob) for prob, _ in inside) / count, 4),
                "observed_rate": round(sum(float(label) for _, label in inside) / count, 4),
                "count": count,
            }
        )
    return points


def expected_calibration_error(
    probabilities: list[float],
    labels: list[int],
    bins: int = 10,
) -> float | None:
    """ECE：各段「预测值与实际发生率之差」的样本加权平均。"""
    curve = calibration_curve(probabilities, labels, bins)
    if not curve:
        return None
    total = sum(point["count"] for point in curve)
    weighted = sum(
        abs(point["mean_predicted"] - point["observed_rate"]) * point["count"] for point in curve
    )
    return round(weighted / total, 6)


__all__ = [
    "brier_score",
    "calibration_curve",
    "expected_calibration_error",
    "log_loss",
]
