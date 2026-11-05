"""大库复现脚本（只用于本地量化，不跑 CI）。

用法（用仓库 .venv，必须从仓库根目录运行）：
    LP_DATA_DIR=/tmp/lp-repro PYTHONPATH=. .venv/bin/python scripts/repro_large_cleanup.py \\
        --rows-per-day 800 --days 780 --measure

--seed 会直接复用已有库（库不存在时先造库），避免反复造数；--no-seed 强制重建。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.schema import PrimaryKeyConstraint, UniqueConstraint


START = date(2024, 8, 1)

# 每张表只按「多少行」分桶；列名、列类型、唯一约束全部来自 SQLAlchemy metadata。
EVERY_5_BUCKET = {
    "limit_up_patterns",
    "pattern_matches",
    "signal_events",
    "signal_snapshots",
}
EVERY_11_BUCKET = {
    "skipped_records",
    "data_quality_errors",
    "intraday_watch_alerts",
    "ai_analysis_reports",
    "ai_pick_results",
    "backtest_observations",
    "stock_training_data",
}
DAY_BUCKET = {
    "trading_days",
    "backtest_runs",
    "signal_outcomes",
    "signal_shadow_status",
    "ai_rankings",
    "ai_daily_summaries",
    "ai_daily_rank_boards",
}


def _iter_table_rows(table_name: str, rows_per_day: int, days: int):
    """产出 (day, code)。code 为空表示这是每日一行的表。"""
    if table_name in DAY_BUCKET:
        for i in range(days):
            yield START + timedelta(days=i), ""
        return

    if table_name in EVERY_5_BUCKET:
        step = 5
    elif table_name in EVERY_11_BUCKET:
        step = 11
    else:
        step = 1

    for i in range(days):
        day = START + timedelta(days=i)
        for stock in range(0, rows_per_day, step):
            yield day, f"{(stock % 5000):06d}"


def _is_auto_increment_pk(column) -> bool:
    return column.primary_key and isinstance(column.type, Integer)


def _is_identity_column(column, table) -> bool:
    for constraint in table.constraints:
        if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint)):
            if column.name in {c.name for c in constraint.columns}:
                return True
    return False


def _column_value(column, table, day: date, code: str) -> object:
    col_type = column.type
    if isinstance(col_type, DateTime):
        return f"{day.isoformat()} 04:00:00"
    if isinstance(col_type, Date):
        return day.isoformat()
    if isinstance(col_type, JSON):
        return "{}"
    if isinstance(col_type, Boolean):
        return 0
    if isinstance(col_type, (Integer, Float)):
        return 0
    if isinstance(col_type, (String, Text)):
        if _is_identity_column(column, table):
            return f"{day.isoformat()}-{code}" if code else day.isoformat()
        return ""
    return ""


def _insert_table(conn: sqlite3.Connection, table, rows_per_day: int, days: int) -> None:
    columns = [column for column in table.columns if not _is_auto_increment_pk(column)]
    names = [column.name for column in columns]
    sql = f'INSERT INTO "{table.name}" ({", ".join(names)}) VALUES ({", ".join("?" for _ in columns)})'
    batch: list[tuple] = []
    for day, code in _iter_table_rows(table.name, rows_per_day, days):
        batch.append(tuple(_column_value(column, table, day, code) for column in columns))
        if len(batch) >= 50_000:
            conn.executemany(sql, batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)


def build_db(path: Path, rows_per_day: int, days: int, seed: int) -> None:
    """用 SQLAlchemy metadata 建完整表，再从 metadata 动态生成列值批量造数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["LP_DATA_DIR"] = str(path.parent)
    os.environ["LP_SCHEDULER"] = "0"
    os.environ["LP_WARM"] = "0"
    os.environ["LP_AUTO_SYNC"] = "0"
    from backend.app.core.database import Base, get_engine, reset_engine
    from backend.app.models import tables  # noqa: F401

    if path.exists():
        path.unlink()
    reset_engine()
    engine = get_engine()
    Base.metadata.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA page_size=4096;
        PRAGMA cache_size=-160000;
        """
    )
    try:
        conn.execute("BEGIN")
        for table in Base.metadata.sorted_tables:
            if table.name == "stocks":
                continue
            _insert_table(conn, table, rows_per_day, days)
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def make_files(root: Path, count: int) -> None:
    """造一些带日期名的旧文件，覆盖预览里会扫描到的目录。"""
    old = (date(2024, 8, 1) + timedelta(days=7)).isoformat()
    for i in range(max(1, count)):
        for name, sub in (
            ("每日总结文件", "summaries"),
            ("每日排名文件", "rankings"),
            ("导出文件", "exports"),
            ("盘中快照", "snapshots"),
            ("信号快照文件", "signal_snapshots"),
            ("聚类结果", "clusters"),
            ("日志目录", "logs"),
        ):
            target = root / sub / f"{old}-{i}-{name}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows-per-day", type=int, default=800)
    ap.add_argument("--days", type=int, default=780)
    ap.add_argument("--files", type=int, default=100)
    ap.add_argument("--root", default="/tmp/lp-large-cleanup")
    ap.add_argument("--keep", action="store_true", help="复用已有数据目录（不删根目录）")
    ap.add_argument("--seed", action="store_true", help="库存在时跳过造数，直接测量")
    ap.add_argument("--no-seed", action="store_true", help="强制重建库后再测量")
    ap.add_argument("--measure", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    if not args.keep and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    if not args.measure:
        t = time.perf_counter()
        if not args.seed and (args.no_seed or not (root / "lp.db").exists()):
            build_db(root / "lp.db", args.rows_per_day, args.days, 7)
        make_files(root, args.files)
        print(f"seeded rows_per_day={args.rows_per_day} days={args.days} in {time.perf_counter()-t:.2f}s")
        print(f"db_size={ (root/'lp.db').stat().st_size/1024/1024:.1f}MB")
        return

    if args.no_seed and (root / "lp.db").exists():
        (root / "lp.db").unlink()
    if not (root / "lp.db").exists():
        build_db(root / "lp.db", args.rows_per_day, args.days, 7)
    make_files(root, args.files)

    os.environ["LP_DATA_DIR"] = str(root)
    os.environ["LP_SCHEDULER"] = "0"
    os.environ["LP_WARM"] = "0"
    os.environ["LP_AUTO_SYNC"] = "0"

    from backend.app.core.database import init_db
    from backend.app.services import data_cleanup
    from backend.app.core.clock import today

    init_db()
    t = time.perf_counter()
    preview = data_cleanup.preview(60)
    preview_elapsed = time.perf_counter() - t
    print(f"preview={preview_elapsed:.2f}s tables={len(preview['tables'])} files={preview['total_files']}")
    t = time.perf_counter()
    job = data_cleanup.start_job(days=60, mode="manual", label="repro-manual")
    start_elapsed = time.perf_counter() - t
    print(f"start_job={start_elapsed:.2f}s job_id={job['job_id']}")
    while True:
        status = data_cleanup.job_status(job["job_id"])
        if not status.get("running"):
            break
        time.sleep(0.5)
    execute_elapsed = status.get("elapsed_seconds", 0.0)
    print(f"execute={execute_elapsed:.2f}s status={status['status']} progress={status.get('progress')} phase={status.get('phase')} message={status.get('message')}")
    print(f"deleted={status.get('result', {}).get('deleted')}")
    print(f"vacuum={status.get('result', {}).get('vacuum')}")
    print(f"today={today()}")


if __name__ == "__main__":
    main()
