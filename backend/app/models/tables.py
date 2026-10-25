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

from backend.app.core.clock import now_naive
from backend.app.core.database import Base


def _now() -> datetime:
    """东八区挂钟（摘掉时区再进那列 `DateTime`）。

    这半行决定了页面上所有「生成于/创建于」是不是同一个时刻：写 UTC 的话，
    回测列表那一批时间看起来永远早八小时。
    """
    return now_naive()


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


class MatchSummary(Base):
    """一只股票某一天对**整个样本池**的汇总：几条样本、平均分、最高分、落哪一档。

    这张表存在的理由是 `pattern_matches` 只留每只股票的前 `MATCH_TOP_N` 条：
    平均分与样本条数要吃的是全池，从截断后的那 20 条重算会得到
    「只在最像的 20 条里的平均分」——那是另一件事，不能顶着同一个名字。
    写这张表的和写那 20 行的是同一次 `score_day`，所以两边的数必然出自同一把尺子。
    """

    __tablename__ = "match_summaries"
    __table_args__ = (UniqueConstraint("target_code", "target_date", name="uq_match_summary"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_code: Mapped[str] = mapped_column(String(6), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    #: 这一轮实际参与比对的样本条数（已剔掉它自己那些条）
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    high_similarity_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 全池平均分。池子为空时是 None，不是 0 分。
    avg_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    band: Mapped[str] = mapped_column(String(16), default="")
    #: 这一轮用的池子覆盖哪几个交易日、样本表里一共几条。
    #: 有这两个数才答得出「页面上这 40 条样本是从哪几天来的」。
    pool_dates: Mapped[list] = mapped_column(JSON, default=list)
    pool_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


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
    """一轮回测的账。`result` 是页面直接读的那份总结，不是中间量。

    只有 `status == "done"` 的那一行会被下游读到（榜上的概率列、预测页的档位发生率）。
    `running` 与 `failed` 都在库里留着：回测是长任务，跑砸了要说得出是哪一天砸的，
    不能变成一个安静的空结果。`finished_at` 由写的那一头填，读取那一律按
    「最近一次 done」的 `created_at` 排序，不认这里的时间。
    """

    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    split_ratio: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(16), default="running")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    #: 这几列是下游要直接读的数，放在 JSON 之外：分档发生率、概率列、预测页都要问
    #: 「这次回测覆盖几天、多少条观测、全样本基准发生率是多少」，
    #: 让每一处各自去 parse `result` 是三个版本的口径。
    day_count: Mapped[int] = mapped_column(Integer, default=0)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 全样本次日涨停率（0~1）。None 表示「一条有结果的观测都没有」，
    #: 那和「基准发生率是 0%」是两件事：前者算不出相对倍数，后者是真有结论。
    baseline_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="")
    #: 跑砸了那一行留下的原因。成功时是空串，不是「成功」。
    error: Mapped[str] = mapped_column(String(500), default="")
    #: 这一轮用的档位下沿、窗口长度、相似度权重。回测数字能不能和上一次比，看的是这里。
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BacktestObservation(Base):
    """一只股票在某一天的「与涨停样本有多像」加上「下一个交易日到底涨没涨停」。

    一天一只一行，覆盖当天全部有效候选：没有前 N 截断，也没有分数门槛。
    截断会让分档发生率变成「只在榜首那几只身上统计出来的发生率」，
    而这一列要回答的恰恰是「落在这一档的股票次日涨停的比例」。
    """

    __tablename__ = "backtest_observations"
    __table_args__ = (
        Index("ix_obs_run", "run_id", "trade_date"),
        Index("ix_obs_band", "run_id", "band"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    trade_date: Mapped[date] = mapped_column(Date)
    stock_code: Mapped[str] = mapped_column(String(6))
    stock_name: Mapped[str] = mapped_column(String(32), default="")
    max_similarity: Mapped[float] = mapped_column(Float)
    avg_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 这一行实际参与比对的样本条数（已剔掉它自己那些条）与相似度达到高档的条数。
    #: 有了这两个数，页面才说得出「92 分是跟 40 条样本比出来的」还是「跟 2 条比出来的」。
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    high_similarity_count: Mapped[int] = mapped_column(Integer, default=0)
    band: Mapped[str] = mapped_column(String(16))
    next_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: null 是「这一天之后库里还没有下一根行情」，区间最后一天必然如此。
    #: 它不计入发生率，只计入 `pending_count`——把没揭晓的当成没涨停，
    #: 等于拿未来的空白压低历史发生率。
    next_limit_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    next_change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)


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
    cost_text: Mapped[str] = mapped_column(String(120), default="")


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
    cost_text: Mapped[str] = mapped_column(String(120), default="")
    error: Mapped[str] = mapped_column(String(1000), default="")


class AiDailySummary(Base):
    """每日 AI 总结。清理任务不许删这张表。"""

    __tablename__ = "ai_daily_summaries"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class IntradayWatchAlert(Base):
    """盘中涨幅报警的账：一天、一只、一档只留一条。

    这张表存在的唯一理由是「每只每天每档只报一次」这条规则必须活过进程重启。
    只记在内存里，绿色版一退出、服务一重开，当天已经涨过 7% 的那几只就会再报一遍、
    窗口再被摞到最前面——那是打扰，不是报警。去重的判据是这句唯一约束，
    不是「服务记得别重复」。

    `fired_at` 与 `acked_at` 都是定长 ISO 串：字典序就是时间序，裁剪和排序按它比较才成立
    （与另两张 AI 表同一取向）。`acked_at` 是这张表唯一的「用户动作」列，空串就是还没点
    「知道了」，而**未确认的置顶档正是桌面版该把窗口摞在最前面那些只**——所以解除置顶
    不是前端的一个布尔变量，是这里的一次写库：页面刷新、窗口重开、服务重启之后，
    「还剩几条没点」仍然答得出来。

    刻意不存盘中每一轮的价格序列：那是采样的中间量，只活在内存的环形缓冲里。
    这张表是派生账，删掉只丢报警历史，随时可以裁（保留窗口见 store）。
    """

    __tablename__ = "intraday_watch_alerts"
    __table_args__ = (
        UniqueConstraint("trade_date", "stock_code", "tier", name="uq_watch_alert"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 报警属于哪一个交易日（判据日，也是去重键之一）。库里这张表建得早，
    #: 日期列一直是 Date，与另两张 AI 表的写法保持一致，不按字符串存。
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    stock_code: Mapped[str] = mapped_column(String(6))
    #: 触发那一刻快照给的名字，抄下来留档：事后改名不影响这条账读得懂
    stock_name: Mapped[str] = mapped_column(String(32), default="")
    tier: Mapped[str] = mapped_column(String(16))
    #: 名次与分数可空：池子口径以后可能换，换到没有名次的口径时宁可空着，也不许写 0 名
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 触发那一刻的当日累计涨幅，它是这条报警的出处；事后拿当前价替代就答不上「凭什么报我」
    percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 第一档不看涨速，所以它是 None 而不是 0.0——「0 个百分点跳升」是一个有内容的结论，
    #: 「这一档压根不靠跳升判定」是另一回事，两者不许混成同一个 0
    rise_3min: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 几点报的。旧库里这一列可能是空的，那种行由 `created_at` 兜住（见 store 的 fired_label）
    fired_at: Mapped[str] = mapped_column(String(32), default="")
    acked_at: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


