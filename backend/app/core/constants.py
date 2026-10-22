"""口径常量。对比窗口固定为 3；明细保留天数默认为 5，可在设置里改。"""

from __future__ import annotations

from datetime import date

# 相似度只和最近 3 个交易日里涨停过的股票比。这个数不能在页面上改。
COMPARE_WINDOW_TRADING_DAYS = 3
SAMPLE_POOL_TRADING_DAYS = COMPARE_WINDOW_TRADING_DAYS

# 行情、标签等明细默认只留最近 5 个交易日。设置页可以改，见 retention 范围。
DETAIL_RETENTION_TRADING_DAYS = 5
RETENTION_MIN_TRADING_DAYS = 3
RETENTION_MAX_TRADING_DAYS = 30

# 三日窗口本身只要 3 根 K 线。明细只留大约一周，不再要求 20 日历史。
MIN_LISTED_TRADING_DAYS = 3
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
    "price": 0.35,
    "kline": 0.20,
    "volume": 0.15,
    "amount": 0.10,
    "turnover": 0.10,
    "position": 0.05,
    "trend": 0.05,
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
