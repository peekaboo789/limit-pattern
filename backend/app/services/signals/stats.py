"""显著性统计：置换检验（经验 p 值，不做正态假设）+ BH/Holm 多重检验校正。只用标准库。"""

from __future__ import annotations

import random
from statistics import mean


def _mean(xs: list[float]) -> float:
    return mean(xs) if xs else 0.0


def two_sample_pvalue(
    treated: list[float],
    control: list[float],
    *,
    n_perm: int = 1000,
    seed: int = 0,
    alternative: str = "greater",
) -> dict:
    """置换检验：把「信号 / 非信号」标签随机打乱 n_perm 次，重算均值差得到经验 p 值。

    alternative:
      greater  -> H1: mean(treated) > mean(control)
      less     -> H1: mean(treated) < mean(control)
      two-sided-> H1: mean(treated) != mean(control)

    返回 observed 统计量、经验 p 值、置换统计量列表、样本量。样本为空时 p 值返回 None。
    """
    if not treated or not control:
        return {
            "statistic": None,
            "p_value": None,
            "n_treated": len(treated),
            "n_control": len(control),
            "n_perm": n_perm,
            "alternative": alternative,
        }
    observed = _mean(treated) - _mean(control)
    pooled = list(treated) + list(control)
    n1 = len(treated)
    rng = random.Random(seed)
    stats: list[float] = []
    for _ in range(n_perm):
        rng.shuffle(pooled)
        stats.append(_mean(pooled[:n1]) - _mean(pooled[n1:]))
    if alternative == "greater":
        extreme = sum(1 for s in stats if s >= observed)
    elif alternative == "less":
        extreme = sum(1 for s in stats if s <= observed)
    else:
        extreme = sum(1 for s in stats if abs(s) >= abs(observed))
    p_value = (extreme + 1) / (n_perm + 1)
    return {
        "statistic": round(observed, 6),
        "p_value": round(p_value, 6),
        "n_treated": len(treated),
        "n_control": len(control),
        "n_perm": n_perm,
        "alternative": alternative,
    }


def sign_pvalue(
    values: list[float],
    *,
    n_perm: int = 1000,
    seed: int = 0,
    alternative: str = "greater",
) -> dict:
    """单样本置换检验：把符号随机翻转 n_perm 次，检验均值是否偏离 0。

    用于「信号超额收益是否显著大于 0」这类单样本问题。
    """
    if not values:
        return {"statistic": None, "p_value": None, "n": 0, "n_perm": n_perm, "alternative": alternative}
    observed = _mean(values)
    rng = random.Random(seed)
    stats: list[float] = []
    for _ in range(n_perm):
        flipped = [v if rng.random() < 0.5 else -v for v in values]
        stats.append(_mean(flipped))
    if alternative == "greater":
        extreme = sum(1 for s in stats if s >= observed)
    elif alternative == "less":
        extreme = sum(1 for s in stats if s <= observed)
    else:
        extreme = sum(1 for s in stats if abs(s) >= abs(observed))
    p_value = (extreme + 1) / (n_perm + 1)
    return {
        "statistic": round(observed, 6),
        "p_value": round(p_value, 6),
        "n": len(values),
        "n_perm": n_perm,
        "alternative": alternative,
    }


def bh_adjusted(pvalues: list[float]) -> list[float]:
    """Benjamini–Hochberg（FDR）。返回与输入等长的调整后 p 值。"""
    n = len(pvalues)
    if n == 0:
        return []
    indexed = sorted((p, i) for i, p in enumerate(pvalues))
    adjusted = [0.0] * n
    running = 1.0
    for rank in range(n - 1, -1, -1):
        p, i = indexed[rank]
        running = min(running, p * n / (rank + 1))
        adjusted[i] = running
    return [round(v, 6) for v in adjusted]


def holm_adjusted(pvalues: list[float]) -> list[float]:
    """Holm（FWER）。返回与输入等长的调整后 p 值。"""
    n = len(pvalues)
    if n == 0:
        return []
    indexed = sorted((p, i) for i, p in enumerate(pvalues))
    adjusted = [0.0] * n
    running = 0.0
    for rank, (p, i) in enumerate(indexed):
        factor = n - rank
        running = max(running, p * factor)
        adjusted[i] = min(1.0, running)
    return [round(v, 6) for v in adjusted]


def significant(pvalues: list[float], *, alpha: float = 0.05, method: str = "bh") -> list[bool]:
    """校正后仍显著（< alpha）的假设。method 取 'bh' 或 'holm'。"""
    if method == "holm":
        adjusted = holm_adjusted(pvalues)
    else:
        adjusted = bh_adjusted(pvalues)
    return [p < alpha for p in adjusted]
