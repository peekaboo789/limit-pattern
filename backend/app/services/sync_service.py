"""后台同步沪深 A 股日线，写完立刻按保留天数清理。"""

from __future__ import annotations

import threading
import time
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
    "phase": "idle",
    "done": 0,
    "total": 0,
    "saved": 0,
    "failed": 0,
}
_PACE_LOCK = threading.Lock()
_NEXT_FETCH = 0.0
_FETCH_GAP = 1 / 16


def status() -> dict:
    with _LOCK:
        snapshot = dict(_STATE)
    snapshot["last_sync_at"] = read_settings_file().get("last_sync_at")
    snapshot["running"] = bool(snapshot["running"])
    snapshot["progress"] = int(snapshot["progress"])
    snapshot["done"] = int(snapshot.get("done") or 0)
    snapshot["total"] = int(snapshot.get("total") or 0)
    snapshot["saved"] = int(snapshot.get("saved") or 0)
    snapshot["failed"] = int(snapshot.get("failed") or 0)
    return snapshot


def reset_state() -> None:
    with _LOCK:
        _STATE.update(
            running=False,
            progress=0,
            message="还没有同步",
            task_id="",
            phase="idle",
            done=0,
            total=0,
            saved=0,
            failed=0,
        )


_FETCH_LIST = fetch_stock_list
_FETCH_KLINE = fetch_kline


def set_fetcher(list_fn=None, kline_fn=None) -> None:
    """测试注入。不传就恢复真实行情源。"""
    global _FETCH_LIST, _FETCH_KLINE
    _FETCH_LIST = list_fn or fetch_stock_list
    _FETCH_KLINE = kline_fn or fetch_kline
    from backend.app.services.history_backfill import clear_calc_memory

    clear_calc_memory()


def _pace() -> None:
    global _NEXT_FETCH
    with _PACE_LOCK:
        now = time.monotonic()
        wait = _NEXT_FETCH - now
        if wait > 0:
            _NEXT_FETCH += _FETCH_GAP
        else:
            wait = 0.0
            _NEXT_FETCH = now + _FETCH_GAP
    if wait > 0:
        time.sleep(wait)


def fetch_kline_retry(code: str, start: date, end: date) -> list[dict]:
    """限速，失败再试两次。给全市场同步和按需补拉共用。"""
    last: Exception | None = None
    for attempt in range(3):
        try:
            _pace()
            return _FETCH_KLINE(code, start, end)
        except Exception as exc:
            last = exc
            time.sleep(0.2 * (2**attempt))
    assert last is not None
    raise last


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
        _set(progress=2, phase="list", message="正在下载股票列表")
        stocks = _FETCH_LIST()
        end = today()
        start = end - timedelta(days=max(20, retention_days() * 3))
        total = len(stocks)
        done = 0
        failed = 0
        saved = 0
        _set(
            progress=8,
            phase="kline",
            total=total,
            done=0,
            saved=0,
            failed=0,
            message=f"股票列表 {total} 只，开始下载日线",
        )

        def _one(item: tuple[str, str]):
            code, name = item
            bars = fetch_kline_retry(code, start, end)
            return code, name, bars

        with ThreadPoolExecutor(max_workers=12) as pool:
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
                progress = 8 + int(80 * done / max(total, 1))
                _set(
                    progress=progress,
                    phase="kline",
                    done=done,
                    total=total,
                    saved=saved,
                    failed=failed,
                    message=f"正在下载日线 {done}/{total}，已写入 {saved} 只，失败 {failed} 只",
                )
        _set(progress=92, phase="cleanup", message="正在清理过期明细")
        purge_expired()
        _refresh_limit_counts()
        summary_note = "，当日总结没有写成"
        try:
            _set(progress=96, phase="summary", message="正在写当日总结")
            from backend.app.services.summary_service import refresh_latest_summary

            if refresh_latest_summary() is not None:
                summary_note = "，已写当日总结"
        except Exception:
            summary_note = "，当日总结没有写成"
        moment = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        stamp("last_sync_at", moment)
        _set(
            running=False,
            progress=100,
            phase="done",
            done=done,
            total=total,
            saved=saved,
            failed=failed,
            message=f"同步完成：写入 {saved} 只，失败 {failed} 只{summary_note}",
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
