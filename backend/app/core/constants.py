"""口径常量。形态特征窗口固定 3 根 K 线；涨停样本池默认近 30 个交易日（设置里 3–60 可改）。"""

from __future__ import annotations

from datetime import date

# 形态特征窗口：涨停前 3 根 K 线（候选股含评估日的最近 3 根）。固定不改。
FEATURE_WINDOW_TRADING_DAYS = 3
COMPARE_WINDOW_TRADING_DAYS = FEATURE_WINDOW_TRADING_DAYS  # 旧名，等同特征窗口
# 量比等用的 5 日基准
VOLUME_BASELINE_TRADING_DAYS = 5

# 涨停样本池：相似度/AI 排名/聚类/次日池/回测只和近 N 个交易日涨停过的股票比。
DEFAULT_SAMPLE_POOL_TRADING_DAYS = 30
SAMPLE_POOL_MIN_TRADING_DAYS = 3
SAMPLE_POOL_MAX_TRADING_DAYS = 60
SAMPLE_POOL_TRADING_DAYS = DEFAULT_SAMPLE_POOL_TRADING_DAYS

# 明细保留：至少 = 样本池 + 特征窗口 + 5 日基准，保证池里最早的涨停也算得出特征。
POOL_HISTORY_BUFFER = FEATURE_WINDOW_TRADING_DAYS + VOLUME_BASELINE_TRADING_DAYS
DETAIL_RETENTION_TRADING_DAYS = 40
RETENTION_MIN_TRADING_DAYS = 3
RETENTION_MAX_TRADING_DAYS = 250

# 三日窗口本身只要 3 根 K 线。明细只留大约一周，不再要求 20 日历史。
MIN_LISTED_TRADING_DAYS = 20  # 完整交易日不足 20 天的不比（旧版 features.py:128）
FULL_HISTORY_TRADING_DAYS = 72

# 相似度：加权欧氏 0.8 + 余弦 0.2
EUCLIDEAN_WEIGHT = 0.8
COSINE_WEIGHT = 0.2

# 分档阈值
BAND_HIGH = 90.0
BAND_MID = 80.0
BAND_LOW = 70.0
BAND_WEAK = 60.0

# 特征组权重，和必须等于 1
GROUP_WEIGHTS: dict[str, float] = {
    "price_trend": 0.35,
    "kline_structure": 0.20,
    "volume": 0.15,
    "amount": 0.10,
    "turnover": 0.10,
    "position": 0.05,
    "three_day_trend": 0.05,
}

# 组内价格权重：close > high > low > open
PRICE_INNER: dict[str, float] = {
    "close": 0.40,
    "high": 0.25,
    "low": 0.20,
    "open": 0.15,
}

# 主板 ST 从这一天起由 5% 放宽到 10%（当天起生效）
ST_MAIN_BOARD_RATIO_RELAXED_DATE = date(2026, 7, 6)

TOP_N_DEFAULT = 10
MATCH_INSERT_BATCH = 20_000

# 向量检索：样本 × 目标达到这个数才尝试 faiss，两边都是精确检索
FAISS_PAIR_THRESHOLD = 50_000

# 盘中节奏，没有开关
FAST_REFRESH_SECONDS = 2
FULL_ROUND_SECONDS = 120
IDLE_EXIT_SECONDS = 300
REALTIME_TOP_N = 50
REALTIME_TOP_N_MIN = 10
REALTIME_TOP_N_MAX = 200
REALTIME_LOOP_SLEEP_SECONDS = 0.5

# 次日预测池
DEFAULT_POOL_SIZE = 200
MAX_POOL_SIZE = 1000
MIN_BAND_OBSERVATIONS = 50

# AI 固定榜
DAILY_FEED_LIMIT = 100
DAILY_PICK_COUNT = 20
AI_SOURCE_MAX = 20
EVIDENCE_ROW_LIMIT = 50
RECOMMEND_COUNT = 3
TEXT_LIMIT = 240
CRITERIA_LIMIT = 600
AI_REPORT_RETENTION_DAYS = 90
AI_REPORT_MAX = 1000
DAILY_RANK_RETENTION_DAYS = 90
DAILY_RANK_RUN_TIMEOUT_SECONDS = 900

# 涨幅报警
ALERT_POPUP_PERCENT = 3.0
ALERT_PIN_PERCENT = 6.0
ALERT_SPIKE_POINTS = 2.0
ALERT_SPIKE_WINDOW_SECONDS = 180
ALERT_POLL_SECONDS = 3.0
ALERT_RETENTION_DAYS = 30

# 预测页三项权重
PREDICTION_WEIGHT_SHAPE = 0.5
PREDICTION_WEIGHT_CARRY = 0.3
PREDICTION_WEIGHT_LIFT = 0.2
PREDICTION_LIFT_CAP = 2.0

PREDICTION_FORMULA = (
    "0.5×前日最高匹配度 + 0.3×相似度加权连板率 + 0.2×档位历史发生率相对基准倍数（2 倍封顶）。"
    "缺了的项，权重摊给其余项。这是历史统计的加权，不是涨停概率。"
)
PREDICTION_DISCLAIMER = "相似度不是涨停概率。这一页不预测涨跌，不构成投资建议。"
