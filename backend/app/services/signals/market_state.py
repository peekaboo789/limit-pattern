"""市场状态分组：按信号日情绪强弱把事件分成几组，比较每组超额收益。

情绪来自已有 ai_context.market_context 的输出（涨停家数、炸板率、涨跌家数比、最高连板）。
分组规则是纯函数，便于测试与复现。
"""

from __future__ import annotations


def state_key(ctx: dict) -> str:
    """把一个市场环境 dict 归成 'hot' / 'neutral' / 'cold'。

    hot：涨停多且炸板率低（赚钱效应强）；cold：涨停少（弱势）；其余 neutral。
    """
    limit_up = int(ctx.get("limit_up") or 0)
    broken_rate = ctx.get("broken_rate")
    if broken_rate is None:
        return "neutral"
    if limit_up >= 50 and float(broken_rate) <= 40:
        return "hot"
    if limit_up < 20:
        return "cold"
    return "neutral"


def group_events(events: list[dict], *, ctx_key: str = "market_state") -> dict[str, list[dict]]:
    """把事件按市场状态分组。事件里的 market_state 已由 state_key 算出。"""
    groups: dict[str, list[dict]] = {}
    for event in events:
        key = event.get(ctx_key) or "unknown"
        groups.setdefault(key, []).append(event)
    return groups


def labeled(events: list[dict], *, market_key: str = "market_context") -> list[dict]:
    """给每个事件打上市场状态标签（写入 'market_state'）。"""
    for event in events:
        event["market_state"] = state_key(event.get(market_key) or {})
    return events
