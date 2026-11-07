"""涨停判定只住在这里。别的模块不许自己写「涨了 10% 就是涨停」。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from backend.app.core.constants import ST_MAIN_BOARD_RATIO_RELAXED_DATE

CENT = Decimal("0.01")
MAIN_RATIO = Decimal("0.10")
GROWTH_RATIO = Decimal("0.20")
BSE_RATIO = Decimal("0.30")
ST_TIGHT_RATIO = Decimal("0.05")

REGISTRATION_BOARDS = {"chinext", "star", "bse"}
# 创业板注册制从这一天起，上市前 5 个交易日不设涨跌幅。
CHINEXT_NO_LIMIT_FROM = date(2020, 8, 24)
# 沪深主板全面注册制后的新股，同样是前 5 个交易日不设涨跌幅。
MAIN_BOARD_NO_LIMIT_FROM = date(2023, 4, 10)


def ratio_for(board: str, is_st: bool, trade_date: date) -> Decimal:
    """比例按行情那一天取。主板 ST 在 2026-07-06 之前是 5%，从那天起是 10%。"""
    if board in {"chinext", "star"}:
        return GROWTH_RATIO
    if board == "bse":
        return BSE_RATIO
    if is_st and trade_date < ST_MAIN_BOARD_RATIO_RELAXED_DATE:
        return ST_TIGHT_RATIO
    return MAIN_RATIO


def generic_ratio(board: str) -> Decimal:
    return ratio_for(board, is_st=False, trade_date=ST_MAIN_BOARD_RATIO_RELAXED_DATE)


def _money(value: float | Decimal | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def limit_up_price(pre_close: float, ratio: Decimal) -> Decimal:
    return (Decimal(str(pre_close)) * (Decimal(1) + ratio)).quantize(CENT, rounding=ROUND_HALF_UP)


def limit_down_price(pre_close: float, ratio: Decimal) -> Decimal:
    return (Decimal(str(pre_close)) * (Decimal(1) - ratio)).quantize(CENT, rounding=ROUND_HALF_UP)


def is_valid_bar(
    open_: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    volume: float | None,
) -> bool:
    """成交额可以缺。腾讯和新浪的日线经常只有量，没有额，不能因此把整天判成无效。"""
    prices = (open_, high, low, close)
    if any(price is None or price <= 0 for price in prices):
        return False
    assert high is not None and low is not None and open_ is not None and close is not None
    if high < low:
        return False
    if high + 1e-6 < max(open_, close) or low - 1e-6 > min(open_, close):
        return False
    if volume is None or volume <= 0:
        return False
    return True


@dataclass
class LimitLabel:
    is_limit_up: bool
    is_limit_down: bool
    limit_up_price: float | None
    limit_down_price: float | None
    limit_up_percent: float | None
    reason: str


def _empty(reason: str) -> LimitLabel:
    return LimitLabel(False, False, None, None, None, reason)


def name_is_st(name: str) -> bool:
    compact = "".join((name or "").split()).upper().replace("＊", "*")
    if not compact:
        return False
    token = compact
    while token.startswith("*"):
        token = token[1:]
    return token.startswith("ST") or token.startswith("S*ST") or token.startswith("SST")


def trading_days_between(trading_days: list[date], start: date, end: date) -> int:
    return sum(1 for day in trading_days if start <= day <= end)


def ipo_without_limit(
    board: str,
    list_date: date | None,
    trade_date: date,
    trading_days: list[date],
) -> bool:
    """上市前 5 个交易日不设涨跌幅。没有上市日期就无法判定，不当成新股。

    科创板自始如此。创业板从 2020-08-24 起，沪深主板从 2023-04-10 起。
    更早上市的股票，上市头几天仍按普通涨跌幅。
    """
    if list_date is None or trade_date < list_date:
        return False
    if board == "star":
        pass
    elif board == "chinext":
        if list_date < CHINEXT_NO_LIMIT_FROM:
            return False
    elif board == "main_board":
        if list_date < MAIN_BOARD_NO_LIMIT_FROM:
            return False
    else:
        return False
    lived = trading_days_between(trading_days, list_date, trade_date)
    return 1 <= lived <= 5


def listed_too_short(
    list_date: date | None,
    trade_date: date,
    trading_days: list[date],
    minimum: int = 20,
) -> bool:
    if list_date is None:
        return False
    lived = trading_days_between(trading_days, list_date, trade_date)
    return lived < minimum


class LimitUpDetector:
    def label_bar(
        self,
        *,
        board: str,
        is_st: bool,
        trade_date: date,
        open_: float | None,
        high: float | None,
        low: float | None,
        close: float | None,
        pre_close: float | None,
        volume: float | None,
        list_date: date | None = None,
        trading_days: list[date] | None = None,
        suspended: bool = False,
    ) -> LimitLabel:
        calendar = trading_days or []
        if close is None or pre_close is None or pre_close <= 0:
            return _empty("invalid_quote")
        if suspended or (volume is not None and volume <= 0 and not is_valid_bar(open_, high, low, close, volume)):
            if volume is not None and volume <= 0:
                return _empty("suspended")
        if not is_valid_bar(open_, high, low, close, volume):
            if volume is not None and volume <= 0:
                return _empty("suspended")
            return _empty("invalid_quote")
        if list_date is not None and trade_date < list_date:
            return _empty("not_listed")
        if ipo_without_limit(board, list_date, trade_date, calendar):
            return _empty("ipo_no_limit")
        # 上市不足 20 日不在这里否决涨停标签。那是形态窗口的门槛，
        # 注册制次新股过了前 5 天之后，收在涨停价上仍然是涨停。

        ratio, _relaxed = self._ratio_for_day(board, is_st, trade_date, close, pre_close)
        up = limit_up_price(pre_close, ratio)
        down = limit_down_price(pre_close, ratio)
        close_cents = _money(close)
        percent = float(ratio * 100)
        return LimitLabel(
            is_limit_up=close_cents >= up,
            is_limit_down=close_cents <= down,
            limit_up_price=float(up),
            limit_down_price=float(down),
            limit_up_percent=percent,
            reason="limit_up" if close_cents >= up else ("limit_down" if close_cents <= down else ""),
        )

    def _ratio_for_day(
        self,
        board: str,
        is_st: bool,
        trade_date: date,
        close: float,
        pre_close: float,
    ) -> tuple[Decimal, bool]:
        """收盘越出收紧边界、却还在板块通用边界里，就当成那天没戴帽。"""
        ratio = ratio_for(board, is_st, trade_date)
        plain = generic_ratio(board)
        if ratio == plain:
            return ratio, False
        up_tight = limit_up_price(pre_close, ratio)
        down_tight = limit_down_price(pre_close, ratio)
        up_plain = limit_up_price(pre_close, plain)
        down_plain = limit_down_price(pre_close, plain)
        close_cents = _money(close)
        outside_tight = close_cents > up_tight or close_cents < down_tight
        inside_plain = down_plain <= close_cents <= up_plain
        if outside_tight and inside_plain:
            return plain, True
        return ratio, False

    def distance_to_limit(
        self,
        *,
        pre_close: float | None,
        latest: float | None,
        limit_price: float | None,
    ) -> float | None:
        """距涨停 =（涨停价 − 最新价）÷ 前收 × 100。已封板是 0，缺输入是 None，不给负数。"""
        if pre_close is None or pre_close <= 0 or latest is None or limit_price is None:
            return None
        if latest >= limit_price:
            return 0.0
        gap = (limit_price - latest) / pre_close * 100
        return round(max(0.0, gap), 2)


def annotate_history(
    rows: list[dict],
    *,
    board: str,
    is_st: bool,
    list_date: date | None,
    trading_days: list[date],
) -> list[dict]:
    """按时间顺序重算一段行情的六列标签。不改价格。前收缺失时用上一根收盘价。"""
    detector = LimitUpDetector()
    ordered = sorted(rows, key=lambda row: row["trade_date"])
    previous_close: float | None = None
    labelled: list[dict] = []
    for row in ordered:
        item = dict(row)
        pre_close = item.get("pre_close")
        if pre_close is None and previous_close is not None:
            pre_close = previous_close
            item["pre_close"] = pre_close
        label = detector.label_bar(
            board=board,
            is_st=is_st,
            trade_date=item["trade_date"],
            open_=item.get("open"),
            high=item.get("high"),
            low=item.get("low"),
            close=item.get("close"),
            pre_close=pre_close,
            volume=item.get("volume"),
            list_date=list_date,
            trading_days=trading_days,
        )
        item["is_limit_up"] = label.is_limit_up
        item["is_limit_down"] = label.is_limit_down
        item["limit_up_price"] = label.limit_up_price
        item["limit_down_price"] = label.limit_down_price
        item["limit_up_percent"] = label.limit_up_percent
        item["limit_up_reason"] = label.reason
        if item.get("close") is not None and pre_close:
            item["change_percent"] = round((item["close"] / pre_close - 1) * 100, 4)
        labelled.append(item)
        if item.get("close") is not None:
            previous_close = item["close"]
    return labelled
