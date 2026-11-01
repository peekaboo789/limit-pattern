"""成本模型：佣金、印花税、滑点、最低佣金。所有费率都是可覆盖常量，便于测试与换规则。"""

from __future__ import annotations

# A 股现行口径（可覆盖）。佣金双向，最低 5 元；印花税只卖出；滑点按成交额比例。
COMMISSION_RATE = 0.00025  # 万 2.5，双边
MIN_COMMISSION = 5.0  # 每笔最低佣金（元）
STAMP_TAX_RATE = 0.0005  # 印花税，只卖出（2023-08-28 起减半为 0.05%）
SLIPPAGE_RATE = 0.001  # 滑点，按成交额比例，买卖各一次

DEFAULT_COST = {
    "commission_rate": COMMISSION_RATE,
    "min_commission": MIN_COMMISSION,
    "stamp_tax_rate": STAMP_TAX_RATE,
    "slippage_rate": SLIPPAGE_RATE,
}


def round_2(value: float) -> float:
    """金额保留到分（与涨跌停价四舍五入一致）。"""
    return round(float(value) + 1e-9, 2)


def commission(amount: float, rate: float = COMMISSION_RATE, minimum: float = MIN_COMMISSION) -> float:
    """单边佣金，含最低佣金。amount 是该笔成交额。"""
    return round_2(max(float(minimum), float(amount) * rate))


def stamp_tax(sell_amount: float, rate: float = STAMP_TAX_RATE) -> float:
    """卖出印花税。"""
    return round_2(float(sell_amount) * rate)


def slippage(amount: float, rate: float = SLIPPAGE_RATE) -> float:
    """单边滑点（买价抬高 / 卖价压低）。"""
    return round_2(float(amount) * rate)


def round_trip_cost(
    buy_amount: float,
    sell_amount: float,
    *,
    cost: dict | None = None,
) -> dict:
    """一次 T+1 往返的全部成本。返回分项与合计、合计占买入额的比例。"""
    cfg = DEFAULT_COST if cost is None else {**DEFAULT_COST, **cost}
    c_rate = float(cfg["commission_rate"])
    minimum = float(cfg["min_commission"])
    st_rate = float(cfg["stamp_tax_rate"])
    sl_rate = float(cfg["slippage_rate"])
    buy_commission = commission(buy_amount, c_rate, minimum)
    sell_commission = commission(sell_amount, c_rate, minimum)
    tax = stamp_tax(sell_amount, st_rate)
    buy_slip = slippage(buy_amount, sl_rate)
    sell_slip = slippage(sell_amount, sl_rate)
    total = round_2(buy_commission + sell_commission + tax + buy_slip + sell_slip)
    return {
        "buy_commission": buy_commission,
        "sell_commission": sell_commission,
        "stamp_tax": tax,
        "slippage": round_2(buy_slip + sell_slip),
        "total": total,
        "total_pct": round(total / buy_amount * 100, 4) if buy_amount else None,
    }


def net_return(
    buy_price: float,
    sell_price: float,
    shares: int = 100,
    *,
    cost: dict | None = None,
) -> dict:
    """一笔买入 100 股（按手）的净收益率（%），已扣全部成本。返回 (净收益, 毛收益, 成本)。"""
    buy_amount = float(buy_price) * shares
    sell_amount = float(sell_price) * shares
    costs = round_trip_cost(buy_amount, sell_amount, cost=cost)
    gross = sell_amount - buy_amount
    net = gross - costs["total"]
    return {
        "net_return_pct": round(net / buy_amount * 100, 4) if buy_amount else 0.0,
        "gross_return_pct": round(gross / buy_amount * 100, 4) if buy_amount else 0.0,
        "cost_pct": costs["total_pct"],
        "costs": costs,
    }
