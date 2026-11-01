"""事件研究：超额收益 = 个股收益 − 同期基准收益。基准用同期全市场等权中位数。

输入是每类信号的一组事件，每个事件给出：信号日、个股在信号日收盘买入、持有 hold 日后的净收益，
以及同期基准（全市场等权中位数，或同市值分组中位数）。统计均值 / 中位数 / 胜率 / 盈亏比 / t 统计量。
"""

from __future__ import annotations

import math
from statistics import mean, median, pstdev


def summary(returns: list[float]) -> dict:
    """一组净收益（%）的描述统计。"""
    if not returns:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "win_rate": None,
            "profit_loss_ratio": None,
            "t_stat": None,
            "std": None,
        }
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0
    std = pstdev(returns)
    t_stat = (mean(returns) / (std / math.sqrt(len(returns)))) if std > 0 else None
    plr = (avg_win / avg_loss) if avg_loss > 0 else None
    return {
        "n": len(returns),
        "mean": round(mean(returns), 4),
        "median": round(median(returns), 4),
        "win_rate": round(len(wins) / len(returns), 4),
        "profit_loss_ratio": round(plr, 4) if plr is not None else None,
        "t_stat": round(t_stat, 4) if t_stat is not None else None,
        "std": round(std, 4),
    }


def excess_returns(
    events: list[dict],
    *,
    return_key: str = "net_return_pct",
    benchmark_key: str = "benchmark_return_pct",
) -> list[float]:
    """每个事件的超额收益 = 事件净收益 − 同期基准净收益。缺基准的事件跳过（保守）。"""
    out: list[float] = []
    for event in events:
        r = event.get(return_key)
        b = event.get(benchmark_key)
        if r is None or b is None:
            continue
        out.append(float(r) - float(b))
    return out


def study(events: list[dict], *, return_key: str = "net_return_pct", benchmark_key: str = "benchmark_return_pct") -> dict:
    """对一个持有期的事件做完整事件研究。"""
    excess = excess_returns(events, return_key=return_key, benchmark_key=benchmark_key)
    raw = [float(e[return_key]) for e in events if e.get(return_key) is not None]
    return {
        "n_events": len(events),
        "n_excess": len(excess),
        "excess": summary(excess),
        "raw": summary(raw),
    }


def multi_hold_study(
    events: list[dict],
    *,
    holds: tuple[int, ...] = (1, 2, 3, 4, 5),
    return_prefix: str = "net_return_pct",
    benchmark_prefix: str = "benchmark_return_pct",
) -> dict[int, dict]:
    """一次算 T+1~T+5 各持有期。事件里的键形如 net_return_pct_1 / benchmark_return_pct_1。"""
    return {
        hold: study(
            events,
            return_key=f"{return_prefix}_{hold}",
            benchmark_key=f"{benchmark_prefix}_{hold}",
        )
        for hold in holds
    }
