"""数据库。SQLite 为默认，PostgreSQL 只换连接串。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.core.config import get_settings

logger = logging.getLogger("limit_pattern.database")


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = get_settings().resolved_database_url()
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


ENGINE = None
SessionLocal = None


def get_engine():
    global ENGINE, SessionLocal
    if ENGINE is None:
        ENGINE = _make_engine()
        SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, expire_on_commit=False, future=True)
    return ENGINE


def reset_engine() -> None:
    """测试换库时用。"""
    global ENGINE, SessionLocal
    if ENGINE is not None:
        ENGINE.dispose()
    ENGINE = None
    SessionLocal = None


def init_db() -> None:
    from backend.app import models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(engine)
    upgrade_existing_tables(engine)


def upgrade_existing_tables(engine=None) -> list[str]:
    """没有迁移框架。缺的列就地补上，旧行保持 NULL。"""
    engine = engine or get_engine()
    added: list[str] = []
    sqlite = str(engine.url).startswith("sqlite")
    expected = {
        "ai_pick_results": {
            "at_composite_score": "FLOAT",
            "at_level": "VARCHAR(16)",
            "at_model_gap": "FLOAT",
            "report_id": "INTEGER",
            "model": "VARCHAR(64)",
            "recommended_at": "VARCHAR(32)",
            "stock_name": "VARCHAR(32)",
            "criteria": "VARCHAR(500)",
            "counter_evidence": "VARCHAR(500)",
            "at_max_similarity": "FLOAT",
            "at_to_limit_percent": "FLOAT",
            "at_change_percent": "FLOAT",
            "actual_is_limit_up": "BOOLEAN",
            "actual_close": "FLOAT",
            "actual_change_percent": "FLOAT",
            "actual_max_change_percent": "FLOAT",
            "actual_source": "VARCHAR(32)",
            "checked_at": "VARCHAR(32)",
        },
        "ai_analysis_reports": {
            "chain": "VARCHAR(16)",
            "metric": "VARCHAR(24)",
            "metric_label": "VARCHAR(64)",
            "window_mode": "VARCHAR(16)",
            "started_at": "VARCHAR(32)",
            "finished_at": "VARCHAR(32)",
            "latency_seconds": "FLOAT",
            "board_rows": "INTEGER",
            "feed_rows": "INTEGER",
            "picks_count": "INTEGER",
            "picks_beyond_limit": "INTEGER",
            "text_truncated": "BOOLEAN",
            "recommended_codes": "JSON",
            "cost_text": "VARCHAR(40)",
        },
        "ai_daily_rank_boards": {
            "base_date": "DATE",
            "window_mode": "VARCHAR(16)",
            "metric": "VARCHAR(24)",
            "metric_label": "VARCHAR(64)",
            "model": "VARCHAR(64)",
            "started_at": "VARCHAR(32)",
            "finished_at": "VARCHAR(32)",
            "latency_seconds": "FLOAT",
            "board_rows": "INTEGER",
            "feed_rows": "INTEGER",
            "picks_count": "INTEGER",
            "picks_beyond_limit": "INTEGER",
            "dropped_codes_count": "INTEGER",
            "rejected_count": "INTEGER",
            "unverified_citations": "INTEGER",
            "cost_text": "VARCHAR(40)",
            "error": "VARCHAR(1000)",
        },
        "daily_quotes": {
            "amount_estimated": "INTEGER" if sqlite else "BOOLEAN",
        },
        "intraday_watch_alerts": {
            "stock_name": "VARCHAR(32)",
            "fired_at": "VARCHAR(32)",
        },
        # 回测那两张表建得早、字段少。旧库里它们必然是空的（此前没有任何写入方），
        # 补齐列只是为了让 `create_all` 跳过已存在表时不会读不到字段。
        "backtest_runs": {
            "day_count": "INTEGER",
            "observation_count": "INTEGER",
            "baseline_rate": "FLOAT",
            "note": "VARCHAR(200)",
            "error": "VARCHAR(500)",
            "parameters": "JSON",
        },
        "backtest_observations": {
            "stock_name": "VARCHAR(32)",
            "avg_similarity": "FLOAT",
            "sample_count": "INTEGER",
            "high_similarity_count": "INTEGER",
            "next_change_percent": "FLOAT",
        },
    }
    with engine.begin() as conn:
        for table, columns in expected.items():
            rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall() if sqlite else []
            if not rows and not sqlite:
                continue
            existing = {row[1] for row in rows}
            if not existing:
                continue
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    added.append(f"{table}.{name}")
    if added:
        logger.info("数据库结构已就地补齐 %s 列：%s", len(added), "、".join(added))
    return added


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert SessionLocal is not None
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """HTTP 依赖只关闭、不提交。写接口要自己 commit。"""
    get_engine()
    assert SessionLocal is not None
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def wal_checkpoint(label: str = "") -> None:
    """收尾把 WAL 边文件截断。即使没有页可搬，也要留下能对账的那一句。"""
    engine = get_engine()
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.connect() as conn:
        row = conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)")).fetchone()
    busy, log_pages, checkpointed = (row or (0, 0, 0))
    if label:
        if checkpointed or log_pages:
            logger.info(
                "WAL checkpoint（%s）：搬进主库 %s 页，边文件页 %s",
                label,
                checkpointed,
                log_pages,
            )
        else:
            logger.info(
                "WAL checkpoint（%s）：页已由自动 checkpoint 搬进主库，边文件已截断成 0 字节",
                label,
            )
    else:
        logger.debug("WAL checkpoint 轮询：busy=%s log=%s moved=%s", busy, log_pages, checkpointed)
