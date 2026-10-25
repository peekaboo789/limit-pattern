"""相似度：RobustScaler + 加权欧氏距离 + 余弦。分数只描述形态像不像，不是概率。

距离那一步走 `algorithms/vector_index.py`：规模够大且装了 faiss 时用 `IndexFlatL2`，
否则用 numpy 的恒等式展开。两条路都是精确检索，分数逐项一致，所以后端不是口径、
也不给页面调——见那个文件的开头。
"""

from __future__ import annotations

import math

import numpy as np

from backend.app.algorithms.features import AGGREGATE_NAMES, DAILY_SPEC, dimension_weights
from backend.app.algorithms.vector_index import VectorIndex
from backend.app.core.constants import (
    BAND_HIGH,
    BAND_LOW,
    BAND_MID,
    BAND_WEAK,
    COSINE_WEIGHT,
    EUCLIDEAN_WEIGHT,
)


def band_of(score: float, floors: tuple[float, float, float, float] | None = None) -> str:
    high, mid, low, weak = floors or (BAND_HIGH, BAND_MID, BAND_LOW, BAND_WEAK)
    if score >= high:
        return f"{_num(high)}~100"
    if score >= mid:
        return f"{_num(mid)}~{_num(high)}"
    if score >= low:
        return f"{_num(low)}~{_num(mid)}"
    if score >= weak:
        return f"{_num(weak)}~{_num(low)}"
    return f"<{_num(weak)}"


def band_label(score: float, floors: tuple[float, float, float, float] | None = None) -> str:
    high, mid, low, weak = floors or (BAND_HIGH, BAND_MID, BAND_LOW, BAND_WEAK)
    if score >= high:
        return "高度相似"
    if score >= mid:
        return "较高相似"
    if score >= low:
        return "一般相似"
    if score >= weak:
        return "低相似"
    return "弱相似"


def _num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def fit_robust_scaler(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """中位数和四分位距。常数维的分母用 1，避免被放大成无穷。"""
    if samples.ndim != 2 or samples.shape[0] == 0:
        raise ValueError("样本矩阵是空的")
    median = np.median(samples, axis=0)
    q75 = np.percentile(samples, 75, axis=0)
    q25 = np.percentile(samples, 25, axis=0)
    iqr = q75 - q25
    iqr = np.where(np.abs(iqr) < 1e-9, 1.0, iqr)
    return median.astype(np.float64), iqr.astype(np.float64)


def transform(matrix: np.ndarray, median: np.ndarray, iqr: np.ndarray) -> np.ndarray:
    return (matrix - median) / iqr


def _as_2d(vectors: np.ndarray) -> np.ndarray:
    array = np.asarray(vectors, dtype=np.float64)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    return array


def score_matrix(
    targets: np.ndarray,
    samples: np.ndarray,
    *,
    weights: np.ndarray | None = None,
    euclidean_weight: float = EUCLIDEAN_WEIGHT,
    cosine_weight: float = COSINE_WEIGHT,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (综合分, 欧氏分, 余弦分)，形状都是 (目标数, 样本数)。"""
    target_matrix = _as_2d(targets)
    sample_matrix = _as_2d(samples)
    if target_matrix.shape[1] != sample_matrix.shape[1]:
        raise ValueError("目标和样本的向量长度不一致")
    median, iqr = fit_robust_scaler(sample_matrix)
    scaled_targets = transform(target_matrix, median, iqr)
    scaled_samples = transform(sample_matrix, median, iqr)
    if weights is None:
        weights = np.asarray(dimension_weights(), dtype=np.float64)
    else:
        weights = np.asarray(weights, dtype=np.float64)
    weight_sum = float(weights.sum())
    if weight_sum <= 0:
        raise ValueError("特征权重之和必须大于 0")
    weights = weights / weight_sum
    root = np.sqrt(weights)
    tw = scaled_targets * root
    sw = scaled_samples * root
    dist = VectorIndex.build(sw, n_queries=int(tw.shape[0])).distances(tw)
    euclidean = 100.0 * np.exp(-dist)

    target_norm = np.linalg.norm(scaled_targets, axis=1)
    sample_norm = np.linalg.norm(scaled_samples, axis=1)
    denom = target_norm[:, None] * sample_norm[None, :]
    dot = scaled_targets @ scaled_samples.T
    cosine = np.ones_like(dot)
    np.divide(dot, denom, out=cosine, where=denom > 1e-12)
    cosine = np.clip(cosine, -1.0, 1.0)
    cosine_score = (cosine + 1.0) / 2.0 * 100.0
    # 两边都接近 0 向量时，上面已经把余弦分成了 100（cosine 初值是 1）
    blended = euclidean_weight * euclidean + cosine_weight * cosine_score
    blended = np.clip(blended, 0.0, 100.0)
    return blended, euclidean, cosine_score


def score_pair(target: list[float] | np.ndarray, sample: list[float] | np.ndarray) -> float:
    matrix, _euclid, _cosine = score_matrix(np.asarray(target), np.asarray(sample))
    return float(matrix[0, 0])


def _day_indexes(local: list[int], aggregates: list[int]) -> list[int]:
    per_day = len(DAILY_SPEC)
    indexes: list[int] = []
    for day in range(3):
        indexes.extend(day * per_day + item for item in local)
    indexes.extend(aggregates)
    return indexes


# 60 起是三日聚合：收益、斜率、波动、量能趋势、振幅和、上涨天数、位置变化。
SHAPE_INDEXES = _day_indexes([0, 1, 2, 3, 4, 5, 6, 7, 16, 17, 18, 19], [60, 61, 62, 64, 65, 66])
VOLUME_INDEXES = _day_indexes([8], [63])
VOLUME_PCT_INDEXES = _day_indexes([9, 10, 14, 15], [])
AMOUNT_INDEXES = _day_indexes([11, 12, 13], [])

# 总分 = 形态 0.60 + 成交量 0.25 + 成交量百分比 0.15
COMPONENT_WEIGHTS = (
    ("shape", SHAPE_INDEXES, 0.60),
    ("volume", VOLUME_INDEXES, 0.25),
    ("volume_pct", VOLUME_PCT_INDEXES, 0.15),
)


def score_components(targets: np.ndarray, samples: np.ndarray) -> dict[str, np.ndarray]:
    """每一项都只在给定样本矩阵上打分。调用方负责先去掉自身和未来样本。"""
    target_matrix = _as_2d(targets)
    sample_matrix = _as_2d(samples)
    parts: dict[str, np.ndarray] = {}
    blended = np.zeros((target_matrix.shape[0], sample_matrix.shape[0]), dtype=np.float64)
    weight_sum = 0.0
    full_weights = np.asarray(dimension_weights(), dtype=np.float64)
    for name, columns, weight in COMPONENT_WEIGHTS:
        cols = np.asarray(columns)
        scores, _euclid, _cosine = score_matrix(
            target_matrix[:, cols],
            sample_matrix[:, cols],
            weights=full_weights[cols],
        )
        parts[name] = scores
        blended += weight * scores
        weight_sum += weight
    parts["score"] = blended / weight_sum
    amount_cols = np.asarray(AMOUNT_INDEXES)
    amount, _euclid, _cosine = score_matrix(
        target_matrix[:, amount_cols],
        sample_matrix[:, amount_cols],
        weights=full_weights[amount_cols],
    )
    #: 两边这一组维度一个数都没有（新浪那一路 `amount` 整列为空，三个成交额特征只能补 0）时，
    #: 零向量对零向量会被 `score_matrix` 读成满分 100。那不是「成交额很像」，那是没数据，
    #: 所以标成 NaN，由 `percent` 在出算法层之前换成 None。总分不吃 amount（见 COMPONENT_WEIGHTS），
    #: 受影响的只有「成交额分」那一列和导出 CSV 的那一格。
    if not np.any(target_matrix[:, amount_cols]) and not np.any(sample_matrix[:, amount_cols]):
        amount = np.full_like(amount, math.nan)
    parts["amount"] = amount
    return parts


def percent(value) -> float | None:
    """分项分数出栈的唯一口径：算不了就是 None，不是 0，也不是 100。

    NaN 不能带出这一层——`json.dumps` 会把它写成裸的 `NaN`，浏览器那句
    `response.json()` 当场解析失败，整个页面变成空白而不是一格空值。
    """
    number = float(value)
    if math.isnan(number):
        return None
    return round(number, 2)


def group_slices() -> dict[str, list[int]]:
    """每个特征组占向量的哪些下标。三日聚合单独成 trend。"""
    per_day = len(DAILY_SPEC)
    slices: dict[str, list[int]] = {group: [] for _name, group in DAILY_SPEC}
    slices["trend"] = []
    for day in range(3):
        for index, (_name, group) in enumerate(DAILY_SPEC):
            slices[group].append(day * per_day + index)
    start = per_day * 3
    slices["trend"] = list(range(start, start + len(AGGREGATE_NAMES)))
    return slices


def group_scores(
    target: np.ndarray,
    sample: np.ndarray,
    median: np.ndarray,
    iqr: np.ndarray,
    weights: np.ndarray,
) -> dict[str, float]:
    scaled_t = (target - median) / iqr
    scaled_s = (sample - median) / iqr
    scores: dict[str, float] = {}
    for group, indexes in group_slices().items():
        idx = np.asarray(indexes)
        w = weights[idx]
        w = w / w.sum()
        diff = scaled_t[idx] - scaled_s[idx]
        dist = math.sqrt(float(np.sum(w * diff * diff)))
        scores[group] = round(100.0 * math.exp(-dist), 2)
    return scores
