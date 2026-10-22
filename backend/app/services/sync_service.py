"""后台同步沪深 A 股日线，写完立刻按保留天数清理。"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from backend.app.core.clock import SHANGHAI, today
from backend.app.core.config import read_settings_file, retention_days, stamp
from backend.app.core.database import session_scope
from backend.app.models.tables import DailyQuote, Stock, TradingDay
from backend.app.providers.eastmoney import fetch_kline, fetch_stock_list
from backend.app.providers.markets import classify
from backend.app.services.limit_up_service import annotate_history, name_is_st
from backend.app.services.retention import purge_expired

_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "progress": 0,
    "message": "还没有同步",
    "task_id": "",
}


def status() -> dict:
    with _LOCK:
        snapshot = dict(_STATE)
    snapshot["last_sync_at"] = read_settings_file().get("last_sync_at")
    snapshot["running"] = bool(snapshot["running"])
    snapshot["progress"] = int(snapshot["progress"])
    return snapshot


def reset_state() -> None:
    with _LOCK:
        _STATE.update(running=False, progress=0, message="还没有同步", task_id="")


_FETCH_LIST = fetch_stock_list
_FETCH_KLINE = fetch_kline


def set_fetcher(list_fn=None, kline_fn=None) -> None:
    """测试注入。不传就恢复真实行情源。"""
    global _FETCH_LIST, _FETCH_KLINE
    _FETCH_LIST = list_fn or fetch_stock_list
    _FETCH_KLINE = kline_fn or fetch_kline


def run_sync_now() -> dict:
    """给测试和手动补数用：在当前线程跑完一轮。"""
    with _LOCK:
        task_id = uuid.uuid4().hex[:12]
        _STATE.update(running=True, progress=0, message="开始同步", task_id=task_id)
    _run(task_id)
    return status()


def start_sync() -> str:
    with _LOCK:
        if _STATE["running"]:
            return str(_STATE["task_id"])
        task_id = uuid.uuid4().hex[:12]
        _STATE.update(running=True, progress=0, message="开始同步", task_id=task_id)
    thread = threading.Thread(target=_run, args=(task_id,), name="lp-sync", daemon=True)
    thread.start()
    return task_id


def _set(**values) -> None:
    with _LOCK:
        _STATE.update(values)


def _run(task_id: str) -> None:
    try:
        source = str(read_settings_file().get("data_source") or "eastmoney")
        if source not in {"eastmoney", "东方财富"}:
            raise RuntimeError(f"这一版只支持 eastmoney 行情，当前数据源是 {source}")
        _set(progress=2, message="正在下载股票列表")
        stocks = _FETCH_LIST()
        end = today()
        start = end - timedelta(days=max(20, retention_days() * 3))
        total = len(stocks)
        done = 0
        failed = 0
        saved = 0
        _set(progress=8, message=f"股票列表 {total} 只，开始下载日线")

        def _one(item: tuple[str, str]):
            code, name = item
            bars = _FETCH_KLINE(code, start, end)
            return code, name, bars

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_one, item) for item in stocks]
            for future in as_completed(futures):
                done += 1
                try:
                    code, name, bars = future.result()
                except Exception:
                    failed += 1
                else:
                    if bars:
                        _save_stock(code, name, bars)
                        saved += 1
                    else:
                        failed += 1
                if done % 20 == 0 or done == total:
                    progress = 8 + int(82 * done / max(total, 1))
                    _set(progress=progress, message=f"已处理 {done}/{total}")
        _set(progress=94, message="正在清理过期明细")
        purge_expired()
        _refresh_limit_counts()
        moment = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        stamp("last_sync_at", moment)
        _set(
            running=False,
            progress=100,
            message=f"同步完成：写入 {saved} 只，失败 {failed} 只",
        )
    except Exception as exc:
        _set(running=False, progress=0, message=str(exc) or "同步失败")


def _save_stock(code: str, name: str, bars: list[dict]) -> None:
    exchange, board = classify(code)
    if exchange not in {"SSE", "SZSE"}:
        return
    labelled = annotate_history(
        bars,
        board=board or "main_board",
        is_st=name_is_st(name),
        list_date=None,
        trading_days=[bar["trade_date"] for bar in bars],
    )
    with session_scope() as session:
        stock = session.get(Stock, code)
        if stock is None:
            stock = Stock(code=code)
            session.add(stock)
        stock.name = name
        stock.exchange = exchange
        stock.board = board or ""
        stock.is_st = name_is_st(name)
        stock.source = "eastmoney"
        existing = {
            row.trade_date: row
            for row in session.query(DailyQuote).filter(DailyQuote.stock_code == code)
        }
        seen: set[date] = set()
        for item in labelled:
            day = item["trade_date"]
            seen.add(day)
            row = existing.get(day)
            if row is None:
                row = DailyQuote(stock_code=code, trade_date=day)
                session.add(row)
            row.open = item.get("open")
            row.high = item.get("high")
            row.low = item.get("low")
            row.close = item.get("close")
            row.pre_close = item.get("pre_close")
            row.volume = item.get("volume")
            row.amount = item.get("amount")
            row.turnover_rate = item.get("turnover_rate")
            row.change_percent = item.get("change_percent", item.get("pct_chg"))
            row.is_limit_up = bool(item.get("is_limit_up"))
            row.is_limit_down = bool(item.get("is_limit_down"))
            row.limit_up_price = item.get("limit_up_price")
            row.limit_down_price = item.get("limit_down_price")
            row.limit_up_percent = item.get("limit_up_percent")
            row.limit_up_reason = item.get("limit_up_reason") or ""
            row.source = "eastmoney"
            if session.get(TradingDay, day) is None:
                session.add(TradingDay(trade_date=day, is_open=True, limit_up_count=0))
        # 这一只股票没再出现的旧日期留给清理任务，不在这里逐日删。


def _refresh_limit_counts() -> None:
    with session_scope() as session:
        days = [row[0] for row in session.query(DailyQuote.trade_date).distinct()]
        for day in days:
            count = (
                session.query(DailyQuote)
                .filter(DailyQuote.trade_date == day, DailyQuote.is_limit_up.is_(True))
                .count()
            )
            record = session.get(TradingDay, day)
            if record is None:
                session.add(TradingDay(trade_date=day, is_open=True, limit_up_count=count))
            else:
                record.limit_up_count = count
