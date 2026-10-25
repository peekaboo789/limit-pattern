"""口径常量。形态窗口固定为 3；对比窗口与明细保留天数是默认值，可在设置里改。"""

from __future__ import annotations

from datetime import date

#: 形态向量取几根 K 线。向量维度（VECTOR_DIM）跟着它走，所以它不能在页面上改。
SHAPE_WINDOW_TRADING_DAYS = 3

#: 对比窗口 = 样本池往回看几个交易日（含基准日）：只和这几天里涨停过的股票比。
#: 这是**默认值**，实际值由 `config.compare_window_days()` 读设置，范围 1..保留天数。
#: 代码里取窗口一律走那个函数，别再直接拿这个常量当口径用。
COMPARE_WINDOW_TRADING_DAYS = 3
COMPARE_WINDOW_MIN_TRADING_DAYS = 1

#: 训练宽表每个标签日要往回看几根全市场 K 线（T-1…T-N）。
#: 这些 K 线只认 `daily_quotes` 里留着的全市场行情，所以保留天数至少 N+1 天才会出训练行。
TRAINING_LOOKBACK_TRADING_DAYS = 5

# 行情、标签等明细保留几个交易日。设置页可以改，但下限跟着训练回看走：
# 再少，训练集就一行都出不来，那是一个看起来能选、选了却让整块功能静默空掉的值。
RETENTION_MIN_TRADING_DAYS = TRAINING_LOOKBACK_TRADING_DAYS + 1
DETAIL_RETENTION_TRADING_DAYS = RETENTION_MIN_TRADING_DAYS
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
#: 每只股票落库的匹配明细条数。老工程把目标×样本的**全部**对子都写进库
#: （实测一轮 1,558,980 行），这里改成只留前 20 条：页面要看的是「最像的那几只」，
#: 连板证据取的是前 20 条（`EVIDENCE_MATCH_LIMIT`），两边都用得到这 20 行；
#: 而全池汇总另有 `match_summaries` 一行，不靠从几十万行里重算。
#: 一轮全市场扫描的行数因此约等于「当日候选数 × 20」，保留窗口见 retention。
MATCH_TOP_N = 20

# 向量检索：样本 × 目标达到这个数才尝试 faiss，两边都是精确检索
FAISS_PAIR_THRESHOLD = 50_000

# 形态聚类（档4）。组名与分组都在相似度打分那同一个加权空间里做，见 algorithms/clustering.py。
#: 少于这个样本数不聚类：3 个样本最多得到「每组一个」的退化结果，没有归纳意义。
CLUSTER_MIN_SAMPLES = 4
#: 自动选 k 的搜索范围。上下限都写死在这里，页面上的那排 chip 读的是这两个数。
CLUSTER_MIN_K = 2
CLUSTER_MAX_K = 6
#: 固定随机种子：同一批样本、同一个 k 必须每次都得到同样的分组。
CLUSTER_RANDOM_STATE = 20260927

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

# AI 分析与固定榜
DAILY_FEED_LIMIT = 100
DAILY_PICK_COUNT = 20
EVIDENCE_ROW_LIMIT = 50
RECOMMEND_COUNT = 3
TEXT_LIMIT = 240
CRITERIA_LIMIT = 600
AI_REPORT_RETENTION_DAYS = 90
AI_REPORT_MAX = 1000
DAILY_RANK_RETENTION_DAYS = 90
#: 整趟墙上时钟的上限。老工程实测「排 20 名」最长要 978.5 秒，
#: 900 秒那版必然把成功的请求判成失败，所以这里给到 1800。
DAILY_RANK_RUN_TIMEOUT_SECONDS = 1800.0
#: 盘中整榜与单股直评的上限。榜只有 50 行，比固定榜短得多。
AI_RUN_TIMEOUT_SECONDS = 600.0
AI_STREAM_STALL_SECONDS = 120.0
AI_STREAM_CONNECT_SECONDS = 15.0
AI_PROGRESS_STEP_CHARS = 200
#: 上游在**做出结论之前**把这趟挡回来（429 并发满、5xx 过载）时，一共试几趟。
#: 这种挡回不产生 token，重发不额外花钱；而「请稍后再试」本来就是让对方等一会儿再敲。
AI_TRANSIENT_RETRY_ATTEMPTS = 3
#: 上游没给 `Retry-After` 时，第 2、3 趟各等多久（秒）。超过 `AI_RETRY_WAIT_LIMIT` 就按上限。
AI_TRANSIENT_RETRY_BACKOFF = (5.0, 15.0)
AI_RETRY_WAIT_LIMIT_SECONDS = 20.0
AI_RAW_EXCERPT_LIMIT = 2000
#: 结果在内存里放多久。数据库里那份存档不受它影响。
AI_REPORT_TTL_SECONDS = 600.0
#: 页面轮询的间隔，也是「读数不靠重进页面」的那一拍。
AI_POLL_SECONDS = 2.0

# 多套大模型网关清单（`ai_sources.json`）
#: 清单里最多能存几条自建网关。这一张表是整份重写的，条数没有上限的话
#: 一次误操作存进上百条，页面既画不完也挑不出要点哪一条。
AI_SOURCE_MAX = 20
#: 名字的最长字数。名字是页面上认网关的唯一线索，也是日志里唯一允许出现的那一列。
AI_SOURCE_NAME_MAX = 40

# 每日 JSON 快照（模型记忆与误判库）
#: 往前数连板时最多看多少个交易日。逐日查库里真有行的日子，窗口开大了纯属浪费。
MEMORY_STREAK_LOOKBACK_DAYS = 15

# 涨幅报警
ALERT_POPUP_PERCENT = 3.0
ALERT_PIN_PERCENT = 6.0
ALERT_SPIKE_POINTS = 2.0
ALERT_SPIKE_WINDOW_SECONDS = 180
ALERT_POLL_SECONDS = 3.0
ALERT_RETENTION_DAYS = 30
#: 环形缓冲留多久 = 跳升窗口 + 一点余量。留太久没意义（判据只看 3 分钟前那一个点），
#: 留太短则永远取不到「3 分钟前」，`rise_3min` 会一直是 None。
ALERT_SAMPLE_KEEP_SECONDS = ALERT_SPIKE_WINDOW_SECONDS + 30.0
#: 池子为空时多久重查一次库。读库可能只是撞上一次写锁，不能一次失败就把这一档判死。
ALERT_POOL_RETRY_SECONDS = 60.0

# 数据质量闸门
#: 涨跌幅判 error 时在板块通用幅度上再放的余量。公开源的四舍五入和盘中口径差异会做出
#: 10.03% 这种数，卡死 ±10% 会把好数据判成脏数据。
QUALITY_CHANGE_TOLERANCE_PERCENT = 1.5
#: 前收盘价与上一根收盘价允许的绝对差（元）。除权除息那一天的接不上由更长的间隔挡掉。
QUALITY_PRE_CLOSE_TOLERANCE = 0.02
#: 一天有行情的股票数低于近期水位这个比例，就判这个源只回了半张表。
QUALITY_UNIVERSE_MIN_RATIO = 0.6
#: 一次脏行清理最多允许删掉全天行情的比例。超过就是检查本身跑坏了，宁可什么都不删。
QUALITY_PURGE_MAX_RATIO = 0.05

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
