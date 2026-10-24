"""五张业务表，加辅助表与 AI 派生账。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from backend.app.core.database import Base


def _now() -> datetime:
    return datetime.utcnow()


class Stock(Base):
    __tablename__ = "stocks"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    exchange: Mapped[str] = mapped_column(String(8), default="")
    board: Mapped[str] = mapped_column(String(16), default="")
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_st: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TradingDay(Base):
    __tablename__ = "trading_days"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    limit_up_count: Mapped[int] = mapped_column(Integer, default=0)


class DailyQuote(Base):
    __tablename__ = "daily_quotes"
    __table_args__ = (
        UniqueConstraint("stock_code", "trade_date", name="uq_quote"),
        Index("ix_quote_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    pre_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_limit_up: Mapped[bool] = mapped_column(Boolean, default=False)
    is_limit_down: Mapped[bool] = mapped_column(Boolean, default=False)
    limit_up_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_down_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_up_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_up_reason: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(32), default="")


class CalcBar(Base):
    """补来的历史日线，只给形态计算用。清理任务会把它和正式行情一起删到保留窗口外。"""

    __tablename__ = "calc_bars"
    __table_args__ = (UniqueConstraint("stock_code", "trade_date", name="uq_calc_bar"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    pre_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)


class DailyFeature(Base):
    __tablename__ = "daily_features"
    __table_args__ = (UniqueConstraint("stock_code", "trade_date", name="uq_feature"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    features: Mapped[dict] = mapped_column(JSON, default=dict)


class LimitUpPattern(Base):
    __tablename__ = "limit_up_patterns"
    __table_args__ = (UniqueConstraint("stock_code", "limit_up_date", name="uq_pattern"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    limit_up_date: Mapped[date] = mapped_column(Date, index=True)
    window_dates: Mapped[list] = mapped_column(JSON, default=list)
    feature_vector: Mapped[list] = mapped_column(JSON, default=list)
    day_details: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PatternMatch(Base):
    __tablename__ = "pattern_matches"
    __table_args__ = (
        Index("ix_match_target", "target_code", "target_date"),
        Index("ix_match_date", "target_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_code: Mapped[str] = mapped_column(String(6))
    target_date: Mapped[date] = mapped_column(Date)
    sample_code: Mapped[str] = mapped_column(String(6))
    sample_limit_date: Mapped[date] = mapped_column(Date)
    similarity_score: Mapped[float] = mapped_column(Float)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)


class SkippedRecord(Base):
    __tablename__ = "skipped_records"
    __table_args__ = (Index("ix_skip", "trade_date", "stage"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date)
    stock_code: Mapped[str] = mapped_column(String(6), default="")
    stage: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(String(200), default="")


class DataQualityError(Base):
    __tablename__ = "data_quality_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    stock_code: Mapped[str] = mapped_column(String(6), default="")
    check_name: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(String(300), default="")


class StockTrainingRow(Base):
    __tablename__ = "stock_training_data"
    __table_args__ = (UniqueConstraint("stock_code", "label_date", name="uq_train"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    label_date: Mapped[date] = mapped_column(Date, index=True)
    cutoff_date: Mapped[date] = mapped_column(Date)
    label_limit_up: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_passed: Mapped[bool] = mapped_column(Boolean, default=True)
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(String(64), default="")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    split_ratio: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(16), default="running")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BacktestObservation(Base):
    __tablename__ = "backtest_observations"
    __table_args__ = (Index("ix_obs_run", "run_id", "trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    stock_code: Mapped[str] = mapped_column(String(6))
    max_similarity: Mapped[float] = mapped_column(Float)
    band: Mapped[str] = mapped_column(String(16))
    next_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_limit_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class AiPickResult(Base):
    """AI 推荐出去的那一只，后来到底封没封板。这张账是「它过去被推荐过」那一列的出处。"""

    __tablename__ = "ai_pick_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    source: Mapped[str] = mapped_column(String(32), default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str] = mapped_column(String(16), default="pending")
    at_composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    at_level: Mapped[str] = mapped_column(String(16), default="")
    at_model_gap: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    report_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    recommended_at: Mapped[str] = mapped_column(String(32), default="")
    stock_name: Mapped[str] = mapped_column(String(32), default="")
    criteria: Mapped[str] = mapped_column(String(500), default="")
    counter_evidence: Mapped[str] = mapped_column(String(500), default="")
    at_max_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    at_to_limit_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    at_change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_is_limit_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    actual_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_max_change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_source: Mapped[str] = mapped_column(String(32), default="")
    checked_at: Mapped[str] = mapped_column(String(32), default="")


class AiAnalysisReport(Base):
    """盘中整榜与单股直评那两次花钱的存档。"""

    __tablename__ = "ai_analysis_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    base_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    model: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    chain: Mapped[str] = mapped_column(String(16), default="")
    metric: Mapped[str] = mapped_column(String(24), default="")
    metric_label: Mapped[str] = mapped_column(String(64), default="")
    window_mode: Mapped[str] = mapped_column(String(16), default="")
    started_at: Mapped[str] = mapped_column(String(32), default="")
    finished_at: Mapped[str] = mapped_column(String(32), default="")
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    board_rows: Mapped[int] = mapped_column(Integer, default=0)
    feed_rows: Mapped[int] = mapped_column(Integer, default=0)
    picks_count: Mapped[int] = mapped_column(Integer, default=0)
    picks_beyond_limit: Mapped[int] = mapped_column(Integer, default=0)
    text_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    recommended_codes: Mapped[list] = mapped_column(JSON, default=list)
    cost_text: Mapped[str] = mapped_column(String(40), default="")


class AiDailyRankBoard(Base):
    """每日 AI 固定榜：`prepared` 那份输入不花钱，`done` 那一次排名才花钱。"""

    __tablename__ = "ai_daily_rank_boards"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_rank_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="prepared")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prepared_at: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    base_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_mode: Mapped[str] = mapped_column(String(16), default="")
    metric: Mapped[str] = mapped_column(String(24), default="")
    metric_label: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[str] = mapped_column(String(32), default="")
    finished_at: Mapped[str] = mapped_column(String(32), default="")
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    board_rows: Mapped[int] = mapped_column(Integer, default=0)
    feed_rows: Mapped[int] = mapped_column(Integer, default=0)
    picks_count: Mapped[int] = mapped_column(Integer, default=0)
    picks_beyond_limit: Mapped[int] = mapped_column(Integer, default=0)
    dropped_codes_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    unverified_citations: Mapped[int] = mapped_column(Integer, default=0)
    cost_text: Mapped[str] = mapped_column(String(40), default="")
    error: Mapped[str] = mapped_column(String(1000), default="")


class AiDailySummary(Base):
    """每日 AI 总结。清理任务不许删这张表。"""

    __tablename__ = "ai_daily_summaries"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class IntradayWatchAlert(Base):
    __tablename__ = "intraday_watch_alerts"
    __table_args__ = (
        UniqueConstraint("trade_date", "stock_code", "tier", name="uq_watch_alert"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    stock_code: Mapped[str] = mapped_column(String(6))
    tier: Mapped[str] = mapped_column(String(16))
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    rise_3min: Mapped[float | None] = mapped_column(Float, nullable=True)
    acked_at: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


