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
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyQuote, Stock, TradingDay
from backend.app.providers.eastmoney import fetch_kline, fetch_stock_list
from backend.app.providers.markets import classify, keep_code
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
    "active_source": "",
}
_PACE_LOCK = threading.Lock()
_NEXT_FETCH = 0.0
_FETCH_GAP = 1 / 16
_SICK_LOCK = threading.Lock()
_SICK: set[str] = set()
_CHAIN: list[str] = []


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
    settings = read_settings_file()
    snapshot["data_source"] = settings.get("data_source") or "auto"
    snapshot["active_source"] = snapshot.get("active_source") or settings.get("active_source") or ""
    #: 上一轮的出数来源账（哪几个源各出了多少只、质量检查记了几条）。没有跑过同步就是 None，
    #: 不是空 dict——「查过且没问题」和「没查过」不能读成同一个数。
    snapshot["last_sync_sources"] = settings.get("last_sync_sources")
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
            active_source="",
        )
    _clear_source_health()


_FETCH_LIST = fetch_stock_list
_FETCH_KLINE = fetch_kline
_OVERRIDE = False
logger = get_logger("sync")


def set_fetcher(list_fn=None, kline_fn=None) -> None:
    """测试注入。不传就恢复真实行情源。"""
    global _FETCH_LIST, _FETCH_KLINE, _OVERRIDE
    if list_fn is None and kline_fn is None:
        _OVERRIDE = False
        _FETCH_LIST = fetch_stock_list
        _FETCH_KLINE = fetch_kline
    else:
        _OVERRIDE = True
        if list_fn is not None:
            _FETCH_LIST = list_fn
        if kline_fn is not None:
            _FETCH_KLINE = kline_fn
    from backend.app.services.history_backfill import clear_calc_memory

    clear_calc_memory()
    _clear_source_health()


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


def _clear_source_health() -> None:
    global _CHAIN
    with _SICK_LOCK:
        _SICK.clear()
    _CHAIN = []


def is_source_outage(exc: BaseException) -> bool:
    """5xx、429、断连。用来决定要不要把这个源标成不可用。"""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    text = str(exc).lower()
    if any(token in text for token in ("429", "限流", "too many", "rate limit", "disconnected")):
        return True
    return "http 5" in text


def _mark_sick(name: str) -> None:
    with _SICK_LOCK:
        _SICK.add(name)


def _is_sick(name: str) -> bool:
    with _SICK_LOCK:
        return name in _SICK


def _source_chain() -> list[str]:
    if _OVERRIDE:
        return []
    if _CHAIN:
        return list(_CHAIN)
    from backend.app.providers.router import source_chain

    configured = str(read_settings_file().get("data_source") or "auto")
    active = str(read_settings_file().get("active_source") or "")
    return source_chain(configured, active.split(",")[0] if active else "")


def fetch_from_chain(code: str, start: date, end: date, chain: list[str] | None = None) -> tuple[str, list[dict]]:
    """逐只换源。某个源一旦 5xx 或限流，后面的股票不再用它。"""
    sources = list(chain or _source_chain())
    last: Exception | None = None
    for name in sources:
        if _is_sick(name):
            continue
        try:
            _pace()
            bars = _kline_of(name)(code, start, end)
            return name, list(bars or [])
        except Exception as exc:
            last = exc
            if is_source_outage(exc):
                _mark_sick(name)
                logger.warning("数据源 %s 返回异常，剩余股票改用下一个源：%s", name, exc)
            else:
                logger.info("数据源 %s 拉取 %s 失败：%s", name, code, exc)
    if last is not None:
        raise last
    return "", []


def average_price(bar: dict) -> float | None:
    high, low, close = bar.get("high"), bar.get("low"), bar.get("close")
    if high and low and close:
        return (float(high) + float(low) + float(close)) / 3
    open_, close = bar.get("open"), bar.get("close")
    if open_ and close:
        return (float(open_) + float(close)) / 2
    if close:
        return float(close)
    return None


def fill_missing_fields(
    code: str,
    bars: list[dict],
    used_source: str,
    chain: list[str] | None = None,
) -> list[dict]:
    """换手率尽量用别的源补。成交额补不到时，用成交量乘均价估算，并标成估算。"""
    if not bars:
        return bars
    sources = [name for name in (chain or _source_chain()) if name and name != used_source and not _is_sick(name)]
    need_amount = any(bar.get("amount") is None for bar in bars)
    need_turnover = any(bar.get("turnover_rate") is None for bar in bars)
    if (need_amount or need_turnover) and sources:
        start = min(bar["trade_date"] for bar in bars if bar.get("trade_date"))
        end = max(bar["trade_date"] for bar in bars if bar.get("trade_date"))
        for name in sources:
            if not need_amount and not need_turnover:
                break
            try:
                _pace()
                extra = _kline_of(name)(code, start, end) or []
            except Exception as exc:
                if is_source_outage(exc):
                    _mark_sick(name)
                continue
            by_day = {item.get("trade_date"): item for item in extra}
            for bar in bars:
                other = by_day.get(bar.get("trade_date"))
                if not other:
                    continue
                if bar.get("amount") is None and other.get("amount") is not None:
                    bar["amount"] = other["amount"]
                    bar["amount_estimated"] = False
                if bar.get("turnover_rate") is None and other.get("turnover_rate") is not None:
                    bar["turnover_rate"] = other["turnover_rate"]
            need_amount = any(bar.get("amount") is None for bar in bars)
            need_turnover = any(bar.get("turnover_rate") is None for bar in bars)
    for bar in bars:
        if bar.get("amount") is None:
            price = average_price(bar)
            volume = bar.get("volume")
            if price and volume:
                bar["amount"] = round(float(price) * float(volume), 2)
                bar["amount_estimated"] = True
            else:
                bar["amount_estimated"] = False
        else:
            bar["amount_estimated"] = bool(bar.get("amount_estimated"))
        bar["source"] = used_source or bar.get("source") or ""
    return bars


def fetch_kline_retry(code: str, start: date, end: date) -> list[dict]:
    """限速，失败再试两次。真实同步时按源回退，并补齐缺失的成交额。"""
    if not _OVERRIDE:
        chain = _source_chain()
        if chain:
            _source, bars = fetch_from_chain(code, start, end, chain)
            return fill_missing_fields(code, bars, _source, chain)
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


def run_sync_now(end: date | None = None) -> dict:
    """给测试和手动补数用：在当前线程跑完一轮。

    `end` 是抓线的最后一天，留空就是今天。启动追赶那条路会把它钉在「已经收盘的那天」，
    免得一开盘就把当天那半根线当成一个完整交易日写进库里。
    """
    with _LOCK:
        task_id = uuid.uuid4().hex[:12]
        _STATE.update(running=True, progress=0, message="开始同步", task_id=task_id)
    _run(task_id, end)
    return status()


def start_sync(end: date | None = None) -> str:
    with _LOCK:
        if _STATE["running"]:
            return str(_STATE["task_id"])
        task_id = uuid.uuid4().hex[:12]
        _STATE.update(running=True, progress=0, message="开始同步", task_id=task_id)
    thread = threading.Thread(target=_run, args=(task_id, end), name="lp-sync", daemon=True)
    thread.start()
    return task_id


def _kline_of(name: str):
    from backend.app.providers.router import _module

    return _module(name).fetch_kline


def _cross_check_sample(active: str) -> None:
    from backend.app.providers.router import cross_check

    with session_scope() as session:
        rows = session.query(DailyQuote).order_by(DailyQuote.trade_date.desc()).limit(40).all()
    picked: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if row.stock_code in seen:
            continue
        seen.add(row.stock_code)
        picked.append({"code": row.stock_code, "close": row.close, "pre_close": row.pre_close})
        if len(picked) >= 5:
            break
    cross_check(active, picked)


def _set(**values) -> None:
    with _LOCK:
        _STATE.update(values)


def _run(task_id: str, end_day: date | None = None) -> None:
    global _CHAIN
    _clear_source_health()
    try:
        from backend.app.providers.router import normalize_source, order_for, probe, source_chain

        configured = normalize_source(str(read_settings_file().get("data_source") or "auto"))
        _set(progress=2, phase="list", message="正在选择数据源")
        if _OVERRIDE:
            active = configured if configured in {"eastmoney", "tencent", "sina"} else "eastmoney"
            stocks = _FETCH_LIST()
        else:
            stocks = None
            active = ""
            errors: list[str] = []
            for name in order_for(configured):
                try:
                    _set(message=f"正在尝试数据源 {name}")
                    stocks = probe(name)
                    active = name
                    global _FETCH_KLINE
                    _FETCH_KLINE = _kline_of(name)
                    break
                except Exception as exc:
                    logger.warning("数据源 %s 不可用：%s", name, exc)
                    errors.append(f"{name}：{exc}")
            if not active or stocks is None:
                raise RuntimeError("行情源都不可用：" + "；".join(errors))
        stamp("active_source", active)
        if not _OVERRIDE:
            _CHAIN = source_chain(configured, active)
        stocks = [(code, name) for code, name in stocks if keep_code(code)]
        _set(active_source=active, message=f"使用数据源 {active}，正在下载股票列表")
        end = end_day or today()
        start = end - timedelta(days=max(20, retention_days() * 3))
        total = len(stocks)
        done = 0
        failed = 0
        saved = 0
        switched = 0
        used_sources: set[str] = set()
        #: 每轮出数来源账：这一轮哪几个源各出了多少只。事后只看见「写入 4300 只」是不够的——
        #: 一半来自备用源意味着主源已经只回半张表，那是要在页面上看得出来的事。
        source_counts: dict[str, int] = {}
        primary = _CHAIN[0] if _CHAIN else active
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
                        source_name = str(bars[0].get("source") or primary or active)
                        _save_stock(code, name, bars, source_name)
                        saved += 1
                        if source_name:
                            used_sources.add(source_name)
                            source_counts[source_name] = source_counts.get(source_name, 0) + 1
                        if primary and source_name and source_name != primary:
                            switched += 1
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
        backfill_note = _backfill_pass()
        if not _OVERRIDE:
            try:
                _cross_check_sample(active)
            except Exception:
                logger.info("交叉校验没有做成")
        #: 查完才写总结：账表里那些 error 说的就是这批行，先落账，页面上「这一天有没有脏数据」
        #: 才有出处。跑砸了要在消息里说破，不能让它变成一个安静的 0。
        _set(progress=94, phase="quality", message="正在做数据质量检查")
        quality: dict = {}
        quality_note = ""
        try:
            quality = _quality_pass(dict(stocks))
            quality_note = (
                f"，质量检查记了 {quality.get('errors')} 条 error、"
                f"{quality.get('warnings')} 条 warning"
            )
        except Exception as exc:
            quality_note = f"，质量检查没有跑成：{exc or type(exc).__name__}"
            logger.exception("质量检查没有跑成")
        #: 派生三层（特征 → 涨停样本 → 匹配扫描）跟着行情一起长。页面上那句
        #: 「这一天库里没有样本」十次有九次不是少了行情，而是没人点过数据管理页那几个重建按钮：
        #: 同步只写 `daily_quotes`，另外三张表过去只有人手点才会动。
        #: 排在质量检查之后，是因为建样本要吃 `error_pairs` 那道闸门。
        _set(progress=94, phase="derive", message="正在补齐特征、涨停样本与匹配扫描")
        derive_note = _derive_pass()
        #: 收盘后那两步收尾（对账 + 每日 JSON 快照）排在同步之后：判据读的是判据日那天的
        #: `is_limit_up`，那个数只有等今天的行情落了库才存在。这一步不敲网关、不花钱。
        memory_note = ""
        try:
            _set(progress=95, phase="memory", message="正在对账并落每日快照")
            memory_note = _memory_pass(saved)
        except Exception as exc:
            memory_note = f"，对账与快照没有跑成：{exc or type(exc).__name__}"
            logger.exception("对账与每日快照没有跑成")
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
        stamp(
            "last_sync_sources",
            {
                "at": moment,
                "active_source": active,
                "chain": list(_CHAIN or ([active] if active else [])),
                "sources": dict(sorted(source_counts.items())),
                "saved": saved,
                "failed": failed,
                "switched": switched,
                "quality_errors": int(quality.get("errors") or 0),
                "quality_warnings": int(quality.get("warnings") or 0),
                "quality_scanned_days": int(quality.get("scanned_days") or 0),
                "quality_skipped": len(quality.get("skipped_checks") or []),
            },
        )
        if used_sources:
            ordered = [name for name in (_CHAIN or [active]) if name in used_sources]
            ordered.extend(sorted(used_sources - set(ordered)))
            stamp("active_source", ",".join(ordered))
        switch_note = f"，其中 {switched} 只改用了后续行情源" if switched else ""
        _set(
            running=False,
            progress=100,
            phase="done",
            done=done,
            total=total,
            saved=saved,
            failed=failed,
            active_source=read_settings_file().get("active_source") or active,
            message=f"同步完成：写入 {saved} 只，失败 {failed} 只{switch_note}{backfill_note}{quality_note}{derive_note}{memory_note}{summary_note}",
        )
    except Exception as exc:
        _set(running=False, progress=0, message=str(exc) or "同步失败")


def _backfill_pass() -> str:
    """样本池里涨停前不足三根的票，在同步收尾时补拉历史。

    首页扫描、相似度、盘中榜都只读库、不联网；它们要用的那几根补拉线就在这里备好。
    补不到的不算同步失败：那几只照样会在扫描结果的「跳过」里列出来。
    """
    from backend.app.core.database import session_scope
    from backend.app.services.retention import quote_dates
    from backend.app.services.similarity_service import backfill_pool

    _set(progress=93, phase="backfill", message="正在为样本池补拉涨停前历史")
    try:
        with session_scope() as session:
            days = quote_dates(session)
            if not days:
                return ""
            result = backfill_pool(session, days[-1])
    except Exception as exc:
        logger.exception("样本池补拉没有跑成")
        return f"，样本池补拉没有跑成：{exc or type(exc).__name__}"
    missing = len(result["failed"])
    return f"，样本池有 {missing} 只涨停票补不齐涨停前三根" if missing else ""


def _derive_pass() -> str:
    """把特征、涨停样本、匹配扫描这三层派生数据补到最新那个已收盘的日子。

    不联网、不敲网关：算的全是库里已有的行情。跑砸了不算同步失败，但要在消息里说破，
    不能让它变成一个安静的 0。槽位被页面上的手动任务占着时这一轮直接让路，
    下一轮同步或下一次开机再补——两边一起写 SQLite 才是真麻烦。
    """
    from backend.app.services import freshness

    try:
        return freshness.derive_pass().get("note", "")
    except Exception as exc:
        logger.exception("自动补齐派生数据没有跑成")
        return f"，自动补齐派生数据没有跑成：{exc or type(exc).__name__}"


def _memory_pass(saved: int) -> str:
    """对账 + 落当天与上一个交易日的两份 JSON，回一句给同步消息用的人话。

    为什么排在同步之后：判据读的是判据日那天的 `is_limit_up`，那个数只有等今天的行情
    落了库才存在。为什么导**两个**日子：当天那份里的 `next_day` 必然是空的（第二天还没开盘），
    等明天再重写今天这一份，配对样本才配得上对——只导当天，样本库会永远停在一半。
    一条行情都没落库（非交易日、或源还没出数）就直接跳过：那天没有判据可对。
    """
    if saved <= 0:
        return "，今天一条行情都没落库，对账与快照跳过"
    from backend.app.services import ai_memory_service

    with session_scope() as session:
        day = ai_memory_service.latest_quote_date(session)
        if day is None:
            return "，库里没有行情日，对账与快照跳过"
        result = ai_memory_service.run_memory(session, day)
    if result["errors"]:
        return f"，对账与快照有 {len(result['errors'])} 项没做成：{('；'.join(result['errors']))[:120]}"
    today_export = result.get("export_today") or {}
    previous = result.get("export_previous") or {}
    return (
        f"，已对账并落 {day.isoformat()} 的快照（样本 {today_export.get('memory_samples', 0)} 条、"
        f"误判 {today_export.get('failure_cases', 0)} 条）"
        + (
            f"，回填 {previous.get('trade_date')}（样本 {previous.get('memory_samples', 0)} 条）"
            if previous
            else "，库里没有更早的行情日可回填"
        )
    )


def _quality_pass(names: dict[str, str]) -> dict:
    """跑一遍质量检查。只写 `data_quality_errors`，删脏行是 `/api/quality/purge` 的活。

    名称那份是本轮股票列表顺手带来的，不用再多敲一次网络。
    """
    from backend.app.services.quality_service import run_checks

    with session_scope() as session:
        return run_checks(session, names=names)


def _save_stock(code: str, name: str, bars: list[dict], source: str = "eastmoney") -> None:
    exchange, board = classify(code)
    if not keep_code(code) or exchange not in {"SSE", "SZSE"}:
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
        stock.source = source or "eastmoney"
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
            row.amount_estimated = bool(item.get("amount_estimated"))
            row.turnover_rate = item.get("turnover_rate")
            row.change_percent = item.get("change_percent", item.get("pct_chg"))
            row.is_limit_up = bool(item.get("is_limit_up"))
            row.is_limit_down = bool(item.get("is_limit_down"))
            row.limit_up_price = item.get("limit_up_price")
            row.limit_down_price = item.get("limit_down_price")
            row.limit_up_percent = item.get("limit_up_percent")
            row.limit_up_reason = item.get("limit_up_reason") or ""
            row.source = str(item.get("source") or source or "eastmoney")
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
