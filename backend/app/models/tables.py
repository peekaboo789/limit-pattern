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
        Index("ix_quote_limit", "is_limit_up", "trade_date"),
        Index("ix_quote_date_limit", "trade_date", "is_limit_up"),
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


class AiAnalysisReport(Base):
    __tablename__ = "ai_analysis_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    base_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    model: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AiDailyRankBoard(Base):
    __tablename__ = "ai_daily_rank_boards"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_rank_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="prepared")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prepared_at: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AiRanking(Base):
    """AI 前 20 榜单。每跑一次存一行，永久保留，清理任务不许删。"""

    __tablename__ = "ai_rankings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    run_at: Mapped[str] = mapped_column(String(40), default="")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


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


class SignalEvent(Base):
    """不可变信号事件：发出那一刻的完整上下文。事后只读、只补标签，验证在事件表上重放。"""

    __tablename__ = "signal_events"
    __table_args__ = (
        UniqueConstraint("signal_id", name="uq_signal_id"),
        Index("ix_signal_day", "signal_date", "signal_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64))
    signal_date: Mapped[date] = mapped_column(Date)
    signal_type: Mapped[str] = mapped_column(String(32))
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SignalSnapshot(Base):
    """信号日快照：未复权日线 + 涨跌停价 + 停牌 + 情绪分组，验证只读快照。"""

    __tablename__ = "signal_snapshots"
    __table_args__ = (UniqueConstraint("trade_date", "fingerprint", name="uq_snapshot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SignalOutcome(Base):
    """信号的结果标签：T+1~T+5 净收益、基准收益、超额收益、可成交判定。事后只补不删。"""

    __tablename__ = "signal_outcomes"
    __table_args__ = (UniqueConstraint("signal_id", "hold", name="uq_outcome"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(String(64), index=True)
    hold: Mapped[int] = mapped_column(Integer)
    buyable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sellable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    net_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    excess_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    settled_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AiCouncilSession(Base):
    """多模型讨论验证：证据包、逐条发言、主席结构化结论。只存公开字段，不存 API Key。"""

    __tablename__ = "ai_council_sessions"
    __table_args__ = (
        Index("ix_council_stock_day", "stock_code", "eval_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    stock_name: Mapped[str] = mapped_column(String(64), default="")
    eval_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    participants: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    statements: Mapped[list] = mapped_column(JSON, default=list)
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    conclusion: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AiCouncilBatch(Base):
    """批量讨论导入与执行。每股实际讨论仍落到 AiCouncilSession。"""

    __tablename__ = "ai_council_batches"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_council_batch_idempotency"),
        Index("ix_council_batch_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(120), default="")
    source_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    eval_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    stage: Mapped[str] = mapped_column(String(40), default="待开始")
    profile_ids: Mapped[list] = mapped_column(JSON, default=list)
    profile_order_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    done_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")
    parser_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AiCouncilBatchItem(Base):
    """批次中的单股任务状态。"""

    __tablename__ = "ai_council_batch_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "stock_code", name="uq_council_batch_stock"),
        Index("ix_council_batch_item_batch", "batch_id", "position"),
        Index("ix_council_batch_item_session", "session_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    stock_code: Mapped[str] = mapped_column(String(6), index=True)
    stock_name: Mapped[str] = mapped_column(String(64), default="")
    source_market: Mapped[str] = mapped_column(String(24), default="")
    status: Mapped[str] = mapped_column(String(24), default="pending")
    stage: Mapped[str] = mapped_column(String(40), default="待开始")
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    probability_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[str] = mapped_column(String(16), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SignalShadowStatus(Base):
    """信号上线状态：影子 / 达标 / 上线 / 降权 / 停用。审计轨迹只增不改。"""

    __tablename__ = "signal_shadow_status"
    __table_args__ = (UniqueConstraint("signal_type", "changed_at", name="uq_shadow"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="shadow")
    reason: Mapped[str] = mapped_column(String(200), default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


