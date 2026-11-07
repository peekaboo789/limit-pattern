"""撮合：T+1、涨跌停封死不可成交、停牌不可成交。纯函数，输入是信号日及其后若干根的日线 dict。

约定：bars 按交易日升序，bars[0] 是信号日，bars[hold] 是卖出日。买入价 / 卖出价都取当日收盘价
（保守口径：就算日内曾打开涨停，也按收盘价成交，不假设能买到更低 / 卖得更高）。
"""

from __future__ import annotations

from backend.app.services.signals import cost as cost_model


def is_suspended(bar: dict) -> bool:
    return not bar.get("volume") or float(bar["volume"]) <= 0


def sealed_limit_up(bar: dict) -> bool:
    """全天封死在涨停价上：盘中从未低于涨停价，收盘在涨停价。这种情况买不进。"""
    limit_up = bar.get("limit_up_price")
    if not limit_up:
        return False
    low = bar.get("low")
    close = bar.get("close")
    if low is None or close is None:
        return False
    return float(low) >= float(limit_up) - 1e-9 and float(close) >= float(limit_up) - 1e-9


def sealed_limit_down(bar: dict) -> bool:
    """全天封死在跌停价上：盘中从未高于跌停价，收盘在跌停价。这种情况卖不出。"""
    limit_down = bar.get("limit_down_price")
    if not limit_down:
        return False
    high = bar.get("high")
    close = bar.get("close")
    if high is None or close is None:
        return False
    return float(high) <= float(limit_down) + 1e-9 and float(close) <= float(limit_down) + 1e-9


def buyable(bar: dict) -> bool:
    """信号日收盘能否买入。停牌或全天封死涨停则不可成交。"""
    if is_suspended(bar):
        return False
    return not sealed_limit_up(bar)


def sellable(bar: dict) -> bool:
    """卖出日收盘能否卖出。停牌或全天封死跌停则不可成交。"""
    if is_suspended(bar):
        return False
    return not sealed_limit_down(bar)


def close_of(bar: dict) -> float | None:
    close = bar.get("close")
    return float(close) if close is not None else None


def outcome(
    bars: list[dict],
    hold: int = 1,
    *,
    cost: dict | None = None,
    shares: int = 100,
) -> dict | None:
    """从信号日收盘买入、持有 hold 个交易日后的净收益。不可成交时返回 None。

    bars 至少要有 hold+1 根（信号日 + 卖出日）。T+1 由 hold >= 1 保证。
    """
    if hold < 1:
        raise ValueError("hold must be >= 1 (T+1)")
    if len(bars) < hold + 1:
        return None
    entry = bars[0]
    exit_ = bars[hold]
    if not buyable(entry) or not sellable(exit_):
        return None
    buy_price = close_of(entry)
    sell_price = close_of(exit_)
    if buy_price is None or sell_price is None or buy_price <= 0:
        return None
    return cost_model.net_return(buy_price, sell_price, shares=shares, cost=cost)


def outcomes(
    bars: list[dict],
    holds: tuple[int, ...] = (1, 2, 3, 4, 5),
    *,
    cost: dict | None = None,
    shares: int = 100,
) -> dict[int, dict | None]:
    """一次算 T+1~T+5 各持有期。键是持有交易日数。"""
    return {hold: outcome(bars, hold, cost=cost, shares=shares) for hold in holds}
