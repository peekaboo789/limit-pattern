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
        connect_args["timeout"] = 30
    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
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


def cleanup_indexes(engine=None) -> list[str]:
    """v0.7.5：清理预览/删除用到的日期列索引。

    老库 create_all 不会补模型后加的 Index；这些 SQL 在启动时统一补齐。
    失败只记录日志，不挡启动（下次启动会再尝试）。
    """
    engine = engine or get_engine()
    if not str(engine.url).startswith("sqlite"):
        return []
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_quote_limit ON daily_quotes (is_limit_up, trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_quote_date_limit ON daily_quotes (trade_date, is_limit_up)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_daily_quotes_trade_date ON daily_quotes (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_calc_bars_trade_date ON calc_bars (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_daily_features_trade_date ON daily_features (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_limit_up_patterns_limit_up_date ON limit_up_patterns (limit_up_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_pattern_matches_target_date ON pattern_matches (target_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_skipped_records_trade_date ON skipped_records (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_data_quality_errors_trade_date ON data_quality_errors (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_trading_days_trade_date ON trading_days (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_intraday_watch_alerts_trade_date ON intraday_watch_alerts (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_signal_events_signal_date ON signal_events (signal_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_signal_snapshots_trade_date ON signal_snapshots (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_ai_analysis_reports_trade_date ON ai_analysis_reports (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_ai_daily_rank_boards_trade_date ON ai_daily_rank_boards (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_ai_pick_results_trade_date ON ai_pick_results (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_backtest_observations_trade_date ON backtest_observations (trade_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_stock_training_data_label_date ON stock_training_data (label_date)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_backtest_runs_created_at ON backtest_runs (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_signal_outcomes_settled_at ON signal_outcomes (settled_at)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_signal_shadow_status_changed_at ON signal_shadow_status (changed_at)",
        "CREATE INDEX IF NOT EXISTS ix_cleanup_ai_rankings_trade_date ON ai_rankings (trade_date)",
    )
    created: list[str] = []
    try:
        with engine.begin() as conn:
            for sql in statements:
                conn.execute(text(sql))
                created.append(sql.split(" ON ")[0].rsplit(" ", 1)[-1])
    except Exception:  # noqa: BLE001
        logger.exception("清理日期列索引创建失败，本次不阻塞启动")
    return created


def init_db() -> None:
    from backend.app import models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(engine)
    upgrade_existing_tables(engine)
    cleanup_indexes(engine)


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
        },
        "daily_quotes": {
            "amount_estimated": "INTEGER" if sqlite else "BOOLEAN",
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
