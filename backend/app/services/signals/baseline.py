"""基准对照：随机信号、简单动量、等权全市场基准。

三者用同一套统计代码，如果对照组也「显著」，说明方法本身有偏 —— 方法学自检。
纯函数，随机种子可复现。
"""

from __future__ import annotations

import random


def equal_weight_benchmark(market_returns: dict[str, list[float]]) -> dict[str, float]:
    """全市场等权：每天取当日所有股票收益的中位数。market_returns: {date: [returns]}。

    返回 {date: 当日中位数收益}，便于与事件日对齐。
    """
    return {day: sorted(rs)[len(rs) // 2] for day, rs in market_returns.items() if rs}


def random_signal_returns(
    dates: list[str],
    market_returns: dict[str, list[float]],
    *,
    size: int = 1,
    seed: int = 0,
) -> list[float]:
    """随机抽样：从每个事件日的全市场收益里随机抽 size 只（有放回），得到一组「假信号」收益。"""
    rng = random.Random(seed)
    out: list[float] = []
    for day in dates:
        rs = market_returns.get(day) or []
        if not rs:
            continue
        out.extend(rng.choice(rs) for _ in range(size))
    return out


def momentum_returns(
    ranked: dict[str, list[float]],
    *,
    top_k: int = 20,
) -> list[float]:
    """简单动量：ranked 的每个键是日期，值是「按动量分数降序排列的当日收益」。

    取每日前 top_k 的收益拼成一组「动量票」收益，与主信号同口径对比。
    """
    out: list[float] = []
    for rs in ranked.values():
        out.extend(rs[:top_k])
    return out
