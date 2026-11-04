"""手动「删除 N 天前数据」：预览、二次确认、先备份、按表/目录清理，最后 VACUUM。

与既有 retention.purge_expired 不同：
- 这是用户在设置页主动触发的自然日删除，不改变默认自动清理行为；
- 永远不动自选股、设置、账号/登录、AI 模型配置、微信会话、当前程序版本；
- 手动删除会动 AI 前 20 榜单等「默认永久保留」表，因为这次是用户明确手动要求；
  新的「每天自动删除 N 天前数据」默认关闭，并且继续保护这些「清理任务不许删」的数据。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import text as sql_text

from backend.app import __version__
from backend.app.core.clock import SHANGHAI, today
from backend.app.core.config import calc_history_days, data_dir, rankings_dir, read_settings_file, summaries_dir
from backend.app.core.database import get_engine, session_scope
from backend.app.core.logging import get_logger
from backend.app.models import tables

logger = get_logger("limit_pattern.data_cleanup")

MIN_DAYS = 30
MAX_DAYS = 365
DEFAULT_DAYS = 60

# 一次只有一个清理/执行任务在跑。手动清理与自动清理也互斥。
_RUN_LOCK = threading.Lock()

DATE_IN_NAME = re.compile(r"(20\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])")


def cleanup_days(raw: dict | None = None) -> int:
    raw = read_settings_file() if raw is None else raw
    try:
        days = int(raw.get("data_cleanup_days") or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, days))


def _validated_days(days: int | None) -> int:
    try:
        value = int(days if days is not None else DEFAULT_DAYS)
    except (TypeError, ValueError) as exc:
        raise ValueError("删除 N 天前数据的天数要是整数") from exc
    if not MIN_DAYS <= value <= MAX_DAYS:
        raise ValueError(f"删除 N 天前数据的天数只能在 {MIN_DAYS}–{MAX_DAYS} 之间")
    return value


def cutoff_date(days: int) -> date:
    return today() - timedelta(days=int(days))


def _table_plans(*, include_permanent: bool) -> list[tuple[str, object, str, str | None]]:
    plans = [
        ("行情日线", tables.DailyQuote, "trade_date", None),
        ("补来的历史日线", tables.CalcBar, "trade_date", None),
        ("形态特征", tables.DailyFeature, "trade_date", None),
        ("涨停明细", tables.LimitUpPattern, "limit_up_date", None),
        ("相似度匹配", tables.PatternMatch, "target_date", None),
        ("跳过记录", tables.SkippedRecord, "trade_date", None),
        ("数据质量错误", tables.DataQualityError, "trade_date", None),
        ("交易日历", tables.TradingDay, "trade_date", None),
        ("盘中报警", tables.IntradayWatchAlert, "trade_date", None),
        ("信号事件", tables.SignalEvent, "signal_date", None),
        ("信号快照表", tables.SignalSnapshot, "trade_date", None),
        ("AI 报告", tables.AiAnalysisReport, "trade_date", None),
        ("每日排名", tables.AiDailyRankBoard, "trade_date", None),
        ("AI 选股结果", tables.AiPickResult, "trade_date", None),
        ("回测观察", tables.BacktestObservation, "trade_date", None),
        ("训练数据", tables.StockTrainingRow, "label_date", None),
        ("回测任务", tables.BacktestRun, "created_at", "datetime"),
        ("信号结果标签", tables.SignalOutcome, "settled_at", "datetime"),
        ("信号上线轨迹", tables.SignalShadowStatus, "changed_at", "datetime"),
    ]
    if include_permanent:
        plans.append(("AI 前 20 榜单", tables.AiRanking, "trade_date", None))
    return plans


PERMANENT_PROTECTED = ("AI 前 20 榜单", "每日总结表")


def _latest_quote_date(session) -> date | None:
    row = session.query(tables.DailyQuote.trade_date).order_by(tables.DailyQuote.trade_date.desc()).first()
    return row[0] if row else None


def _calc_cutoff(session) -> date | None:
    latest = _latest_quote_date(session)
    if latest is None:
        return None
    from backend.app.core.calendar import previous_trading_day

    cursor = latest
    count = max(calc_history_days(), 1)
    for _ in range(count - 1):
        cursor = previous_trading_day(cursor)
    return cursor


def _datetime_cutoff(cutoff: date) -> datetime:
    local = datetime.combine(cutoff, time.min, tzinfo=SHANGHAI)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _table_count(session, label: str, model, column: str, kind: str | None, cutoff: date, calc_cutoff: date | None) -> int:
    column_obj = getattr(model, column)
    if label == "补来的历史日线" and calc_cutoff is not None:
        threshold = min(cutoff, calc_cutoff)
        return session.query(model).filter(column_obj < threshold).count()
    if kind == "datetime":
        return session.query(model).filter(column_obj < _datetime_cutoff(cutoff)).count()
    return session.query(model).filter(column_obj < cutoff).count()


def _archive_estimate(session, cutoff: date, calc_cutoff: date | None) -> int:
    if calc_cutoff is None or calc_cutoff >= cutoff:
        return 0
    have = {
        (stock_code, trade_date)
        for stock_code, trade_date in session.query(tables.CalcBar.stock_code, tables.CalcBar.trade_date).filter(
            tables.CalcBar.trade_date >= calc_cutoff, tables.CalcBar.trade_date < cutoff
        )
    }
    rows = (
        session.query(tables.DailyQuote)
        .filter(tables.DailyQuote.trade_date >= calc_cutoff, tables.DailyQuote.trade_date < cutoff)
        .all()
    )
    return sum(
        1
        for row in rows
        if (row.stock_code, row.trade_date) not in have
        and None not in (row.open, row.high, row.low, row.close)
    )


def _file_old(path: Path, cutoff: date) -> bool:
    match = DATE_IN_NAME.search(path.name)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))) < cutoff
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI).date() < cutoff
    except OSError:
        return False


def _max_mtime(path: Path) -> datetime:
    newest = datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI)
    if path.is_dir():
        for child in path.rglob("*"):
            try:
                child_mtime = datetime.fromtimestamp(child.stat().st_mtime, SHANGHAI)
                newest = max(newest, child_mtime)
            except OSError:
                continue
    return newest


def _scan_directory_files(label: str, root: Path, cutoff: date, recursive: bool = True) -> list[tuple[Path, int]]:
    if not root.exists():
        return []
    iterator = root.rglob("*") if recursive else root.glob("*")
    out = []
    for path in iterator:
        if path.is_file() and _file_old(path, cutoff):
            try:
                out.append((path, path.stat().st_size))
            except OSError:
                continue
    return out


def _scan_log_root(cutoff: date) -> list[tuple[Path, int]]:
    root = data_dir()
    out = []
    for path in root.glob("*"):
        if path.is_file() and ("log" in path.name.lower() or path.name.startswith("app.")) and _file_old(path, cutoff):
            try:
                out.append((path, path.stat().st_size))
            except OSError:
                continue
    return out


def _scan_update_files(cutoff: date) -> list[tuple[Path, int]]:
    root = data_dir() / "updates"
    if not root.exists():
        return []
    out = []
    preserve = {"state.json", "last_result.json", "pending.json"}
    for path in root.iterdir():
        if path.name in preserve:
            continue
        if path.is_dir():
            if path.name == f"v{__version__}" or not path.name.startswith("v"):
                continue
            if _max_mtime(path).date() >= cutoff:
                continue
            size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
            out.append((path, size))
            continue
        if path.is_file() and _file_old(path, cutoff):
            try:
                out.append((path, path.stat().st_size))
            except OSError:
                continue
    return out


def _file_plan_for_cutoff(cutoff: date) -> dict:
    groups = []
    all_files = []
    for label, root, recursive in (
        ("每日总结文件", summaries_dir(), True),
        ("每日排名文件", rankings_dir(), True),
        ("导出文件", data_dir() / "exports", True),
        ("盘中快照", data_dir() / "snapshots", True),
        ("信号快照文件", data_dir() / "signal_snapshots", True),
        ("聚类结果", data_dir() / "clusters", True),
        ("日志目录", data_dir() / "logs", True),
    ):
        files = _scan_directory_files(label, root, cutoff, recursive)
        groups.append({"label": label, "count": len(files), "size": sum(size for _, size in files)})
        all_files.extend(files)
    log_files = _scan_log_root(cutoff)
    groups.append({"label": "根目录日志", "count": len(log_files), "size": sum(size for _, size in log_files)})
    all_files.extend(log_files)
    update_files = _scan_update_files(cutoff)
    groups.append({"label": "updates 旧包", "count": len(update_files), "size": sum(size for _, size in update_files)})
    all_files.extend(update_files)
    return {"groups": groups, "files": all_files, "count": len(all_files), "size": sum(size for _, size in all_files)}


def _protected_for(mode: str) -> list[str]:
    protected = ["自选股", "设置", "账号/登录", "AI 模型配置", "企业微信/个人微信会话", "当前在用的程序版本"]
    if mode == "auto":
        protected.extend(["AI 前 20 榜单", "每日总结表"])
    return protected


def preview(days: int | None = None, *, mode: str = "manual") -> dict:
    days = _validated_days(days) if days is not None else cleanup_days()
    cutoff = cutoff_date(days)
    with session_scope() as session:
        calc_cutoff = _calc_cutoff(session)
        table_rows = []
        for label, model, column, kind in _table_plans(include_permanent=mode == "manual"):
            table_rows.append({
                "label": label,
                "count": _table_count(session, label, model, column, kind, cutoff, calc_cutoff),
            })
        archived = _archive_estimate(session, cutoff, calc_cutoff)
    file_plan = _file_plan_for_cutoff(cutoff)
    return {
        "today": today().isoformat(),
        "days": days,
        "cutoff": cutoff.isoformat(),
        "timezone": "Asia/Shanghai",
        "mode": mode,
        "calc_cutoff": calc_cutoff.isoformat() if calc_cutoff else None,
        "calc_history_days": calc_history_days(),
        "calc_protected_archive": archived,
        "calc_note": "如果自然日删除线晚于计算所需历史窗口，程序会先把这段正式日线转存到 calc_bars；形态计算（position_60 等）不受影响。",
        "tables": table_rows,
        "files": file_plan["groups"],
        "total_files": file_plan["count"],
        "total_file_size": file_plan["size"],
        "protected": _protected_for(mode),
    }


def _verify_backup(backup: Path) -> None:
    try:
        is_dir = backup.is_dir()
    except AttributeError as exc:
        raise RuntimeError(f"备份目录不存在：{backup}") from exc
    if not is_dir:
        raise RuntimeError(f"备份目录不存在：{backup}")
    db = backup / "lp.db"
    if not db.is_file() or db.stat().st_size == 0:
        raise RuntimeError(f"备份数据库不存在或为空：{db}")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        result = conn.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"备份数据库校验失败：{result[0] if result else '无结果'}")


def _delete_table_rows(session, cutoff: date, calc_cutoff: date | None, include_permanent: bool) -> dict[str, int]:
    deleted: dict[str, int] = {}
    if calc_cutoff is not None and calc_cutoff < cutoff:
        have = {(c, d) for c, d in session.query(tables.CalcBar.stock_code, tables.CalcBar.trade_date).filter(
            tables.CalcBar.trade_date >= calc_cutoff, tables.CalcBar.trade_date < cutoff)}
        rows = session.query(tables.DailyQuote).filter(tables.DailyQuote.trade_date >= calc_cutoff, tables.DailyQuote.trade_date < cutoff).all()
        batch = [
            dict(stock_code=r.stock_code, trade_date=r.trade_date, open=r.open, high=r.high, low=r.low, close=r.close,
                 pre_close=r.pre_close, volume=r.volume, amount=r.amount, turnover_rate=r.turnover_rate)
            for r in rows
            if (r.stock_code, r.trade_date) not in have and None not in (r.open, r.high, r.low, r.close)
        ]
        if batch:
            session.bulk_insert_mappings(tables.CalcBar, batch)
            session.flush()
        deleted["archived_to_calc_bars"] = len(batch)
    else:
        deleted["archived_to_calc_bars"] = 0

    for label, model, column, kind in _table_plans(include_permanent=include_permanent):
        column_obj = getattr(model, column)
        if label == "行情日线":
            q = session.query(model).filter(column_obj < cutoff)
        elif label == "补来的历史日线":
            threshold = min(cutoff, calc_cutoff) if calc_cutoff is not None else cutoff
            q = session.query(model).filter(column_obj < threshold)
        elif kind == "datetime":
            q = session.query(model).filter(column_obj < _datetime_cutoff(cutoff))
        else:
            q = session.query(model).filter(column_obj < cutoff)
        deleted[label] = q.delete(synchronize_session=False)
    return deleted


def _delete_files(cutoff: date) -> dict:
    plan = _file_plan_for_cutoff(cutoff)
    for path, _size in plan["files"]:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            logger.warning("删除失败：%s", path)
    _remove_empty_dirs()
    return {"deleted_files": plan["count"], "deleted_file_size": plan["size"]}


def _remove_empty_dirs() -> None:
    for root_name in ("summaries", "rankings", "exports", "snapshots", "signal_snapshots", "clusters", "logs"):
        root = data_dir() / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                try:
                    path.rmdir()
                except OSError:
                    pass


def _db_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _sqlite_pages() -> tuple[int, int]:
    engine = get_engine()
    if not str(engine.url).startswith("sqlite"):
        return 0, 0
    with engine.connect() as conn:
        pages = conn.execute(sql_text("PRAGMA page_count")).fetchone()
        free = conn.execute(sql_text("PRAGMA freelist_count")).fetchone()
    return int((pages or (0,))[0]), int((free or (0,))[0])


def _vacuum() -> dict:
    engine = get_engine()
    if not str(engine.url).startswith("sqlite"):
        return {"vacuum": False, "reason": "非 SQLite，跳过 VACUUM"}
    db_path = data_dir() / "lp.db"
    before = _db_size(db_path)
    before_pages, before_free = _sqlite_pages()
    try:
        with engine.connect() as conn:
            conn.execute(sql_text("VACUUM"))
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("VACUUM 失败：%s", exc)
        return {
            "vacuum": False,
            "error": str(exc),
            "before_size": before,
            "before_page_count": before_pages,
            "before_freelist_count": before_free,
        }
    after = _db_size(db_path)
    after_pages, after_free = _sqlite_pages()
    return {
        "vacuum": True,
        "before_size": before,
        "after_size": after,
        "reclaimed_size": max(0, before - after),
        "before_page_count": before_pages,
        "after_page_count": after_pages,
        "before_freelist_count": before_free,
        "after_freelist_count": after_free,
    }


def execute(days: int | None = None, *, label: str = "manual-cleanup", mode: str = "manual") -> dict:
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("已有删除任务正在执行，请稍后再试")
    try:
        from backend.app.services import backups

        days = _validated_days(days) if days is not None else cleanup_days()
        cutoff = cutoff_date(days)
        backup = backups.make_backup(f"pre-{label}")
        _verify_backup(backup)
        include_permanent = mode == "manual"
        with session_scope() as session:
            calc_cutoff = _calc_cutoff(session)
            deleted = _delete_table_rows(session, cutoff, calc_cutoff, include_permanent)
        file_result = _delete_files(cutoff)
        vacuum = _vacuum()
        return {
            "today": today().isoformat(),
            "days": days,
            "cutoff": cutoff.isoformat(),
            "timezone": "Asia/Shanghai",
            "mode": mode,
            "calc_cutoff": calc_cutoff.isoformat() if calc_cutoff else None,
            "backup": backup.name,
            "deleted": deleted,
            **file_result,
            "vacuum": vacuum,
            "protected": _protected_for(mode),
        }
    finally:
        _RUN_LOCK.release()


def auto_cleanup_if_enabled() -> dict:
    raw = read_settings_file()
    if raw.get("data_cleanup_auto_enabled") is not True:
        return {"ran": False}
    result = execute(cleanup_days(raw), label="auto-cleanup", mode="auto")
    result["ran"] = True
    return result
