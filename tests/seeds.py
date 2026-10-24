"""档3 判据共用的造数夹具：一段带涨停的沪深日线，直接写进临时库。

形状与 `test_backtest_probability.py` 里那份一致（同样的 `weekdays` / `_series` / `_save`），
抽出来是因为特征、样本、匹配、训练四张表都要同一段历史，各抄一遍迟早漂：
一处改了涨停分布，另一处的断言就在讲另一个故事。

这里只造行情与股票主档，**不碰** `daily_features` / `limit_up_patterns` / `pattern_matches` /
`stock_training_rows`——那四张表正是被测对象，夹具提前替它们建好行就等于把被测路径绕过去了。
"""

from __future__ import annotations

from datetime import date, timedelta

from backend.app.core.database import session_scope
from backend.app.models.tables import DailyQuote, Stock, TradingDay
from backend.app.services.limit_up_service import annotate_history, limit_up_price, ratio_for

#: 夹具的最后一个交易日。固定在 2026-06-12（周五），让所有判据吃同一段日历。
END = date(2026, 6, 12)


def weekdays(count: int, end: date = END) -> list[date]:
    """从 `end` 往前数 `count` 个交易日（只排工作日的粗糙日历，够测试用）。"""
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def series(days: list[date], *, limit_on: set[int], start: float = 10.0) -> list[dict]:
    """一段缓缓上行的收盘线，`limit_on` 是那些天的下标（0 是区间第一天）。"""
    price = start
    rows: list[dict] = []
    for index, day in enumerate(days):
        pre = round(price, 2)
        if index in limit_on:
            close = float(limit_up_price(pre, ratio_for("main_board", False, day)))
            high = close
            low = pre
        else:
            close = round(pre * (1.005 + 0.002 * (index % 3)), 2)
            high = round(close * 1.004, 2)
            low = pre
        rows.append(
            {
                "trade_date": day,
                "open": pre,
                "high": high,
                "low": low,
                "close": close,
                "pre_close": pre,
                "volume": 1000 + index * 137,
                "amount": 10000 + index * 91,
                "turnover_rate": 1.2 + index / 10,
            }
        )
        price = close
    return rows


def save_stock(code: str, name: str, exchange: str, rows: list[dict]) -> None:
    """按主档口径打上涨跌停标记后写进库里，顺带把 `trading_days` 补齐。"""
    board = "main_board"
    labelled = annotate_history(
        rows, board=board, is_st=False, list_date=None, trading_days=[row["trade_date"] for row in rows]
    )
    with session_scope() as session:
        stock = session.get(Stock, code)
        if stock is None:
            stock = Stock(code=code)
            session.add(stock)
        stock.name = name
        stock.exchange = exchange
        stock.board = board
        for item in labelled:
            session.add(
                DailyQuote(
                    stock_code=code,
                    trade_date=item["trade_date"],
                    open=item["open"],
                    high=item["high"],
                    low=item["low"],
                    close=item["close"],
                    pre_close=item["pre_close"],
                    volume=item["volume"],
                    amount=item["amount"],
                    turnover_rate=item["turnover_rate"],
                    change_percent=item.get("change_percent"),
                    is_limit_up=item["is_limit_up"],
                    is_limit_down=item["is_limit_down"],
                    limit_up_price=item["limit_up_price"],
                    limit_down_price=item["limit_down_price"],
                    limit_up_percent=item["limit_up_percent"],
                    limit_up_reason=item["limit_up_reason"],
                    source="test",
                )
            )
            if session.get(TradingDay, item["trade_date"]) is None:
                session.add(TradingDay(trade_date=item["trade_date"], is_open=True, limit_up_count=0))


#: 四只票的涨停分布：池子有样本、候选有得比、区间最后一天留给人当「还没揭晓的那天」。
UNIVERSE = (
    ("600000", "浦发银行", "SSE", {4, 7, 9}, 10.0),
    ("600001", "邯郸钢铁", "SSE", {5, 8}, 6.0),
    ("000001", "平安银行", "SZSE", set(), 8.0),
    ("000002", "万科A", "SZSE", {6}, 14.0),
)


def seed(count: int = 12, *, limit_indices: set[int] | None = None) -> list[date]:
    """写四只票的日线，返回那段交易日（升序）。

    `limit_indices` 给定时用它替换默认分布的下标（长历史夹具要把涨停挪到区间后段，
    否则前面的样本会被保留窗口外的日期吃掉）。
    """
    days = weekdays(count)
    for index, (code, name, exchange, limits, start) in enumerate(UNIVERSE):
        picked = limits if limit_indices is None else {value + index for value in limit_indices}
        save_stock(code, name, exchange, series(days, limit_on=picked, start=start))
    return days
