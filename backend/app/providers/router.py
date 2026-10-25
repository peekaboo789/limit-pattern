"""按设置选择行情源。auto 时主源失败或超时就换下一个，并记下真正用到的源。"""

from __future__ import annotations

from datetime import timedelta

from backend.app.core.clock import today
from backend.app.core.logging import get_logger
from backend.app.providers.eastmoney import ProviderError
from backend.app.providers.markets import classify
from backend.app.services.limit_up_service import limit_up_price, ratio_for

logger = get_logger("providers")

SOURCE_ORDER = ("eastmoney", "tencent", "sina")
SOURCE_OPTIONS = ("auto",) + SOURCE_ORDER


def normalize_source(name: str | None) -> str:
    text = (name or "auto").strip().lower()
    if text in {"东方财富"}:
        return "eastmoney"
    if text not in SOURCE_OPTIONS:
        raise ValueError("数据源只能是 auto、eastmoney、tencent、sina")
    return text


def _module(name: str):
    if name == "eastmoney":
        from backend.app.providers import eastmoney

        return eastmoney
    if name == "tencent":
        from backend.app.providers import tencent

        return tencent
    if name == "sina":
        from backend.app.providers import sina

        return sina
    raise ProviderError(f"没有这个数据源：{name}")


def order_for(configured: str) -> list[str]:
    name = normalize_source(configured)
    if name == "auto":
        return list(SOURCE_ORDER)
    return [name]


def source_chain(configured: str, active: str = "") -> list[str]:
    """auto 时从当前源开始，后面接着其余源，供逐只回退。指定单一源时不换。"""
    order = order_for(configured)
    if len(order) == 1:
        return order
    chain: list[str] = []
    if active in SOURCE_ORDER:
        chain.append(active)
    for name in order:
        if name not in chain:
            chain.append(name)
    return chain


def probe(name: str) -> list[tuple[str, str]]:
    module = _module(name)
    stocks = module.fetch_stock_list()
    if not stocks:
        raise ProviderError(f"{name} 股票列表是空的")
    end = today()
    start = end - timedelta(days=10)
    module.fetch_kline(stocks[0][0], start, end)
    return stocks


def cross_check(active: str, rows: list[dict]) -> list[str]:
    """用另一个源抽几只，对一下收盘价、昨收和涨停价。对不上只记日志。"""
    notes: list[str] = []
    sample = [row for row in rows if row.get("code") and row.get("close") and row.get("pre_close")][:5]
    others = [name for name in SOURCE_ORDER if name != active]
    if not sample or not others:
        return notes
    other = others[0]
    try:
        spots = _module(other).fetch_spot([row["code"] for row in sample])
    except Exception as exc:
        logger.info("交叉校验没有做成，%s 不可用：%s", other, exc)
        return notes
    by_code = {item["code"]: item for item in spots}
    for row in sample:
        spot = by_code.get(row["code"])
        if not spot:
            continue
        for field in ("close", "pre_close"):
            left, right = row.get(field), spot.get(field)
            if left is None or right is None:
                continue
            if abs(float(left) - float(right)) > 0.02:
                notes.append(f"交叉校验不一致 {row['code']} {field} {active}={left} {other}={right}")
        quoted_limit = spot.get("limit_up_price")
        if quoted_limit is None or row.get("pre_close") is None:
            continue
        _exchange, board = classify(row["code"])
        if not board:
            continue
        expected = float(limit_up_price(float(row["pre_close"]), ratio_for(board, False, today())))
        if abs(expected - float(quoted_limit)) > 0.02:
            notes.append(f"交叉校验不一致 {row['code']} 涨停价 按昨收计算={expected} {other}报价={quoted_limit}")
    for note in notes:
        logger.warning(note)
    return notes
