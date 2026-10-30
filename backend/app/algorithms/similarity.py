"""相似度引擎，按旧版文档第五节实现：

z_k = (x_k − 中位数_k) / IQR_k × √w_k      （中位数、IQR 在样本池上拟合；IQR ≤ 1e-9 的列按 1；缺数 → 0，即中位数）
d   = ‖z_i − z_j‖₂，scale = 样本池内两两距离的中位数
欧氏分 = 100 · exp(−(d/scale)²)；余弦分 = (cos + 1)/2 · 100（任一范数 ≤ 1e-9 时 cos 记 0）
score  = round(clip(0.8·欧氏分 + 0.2·余弦分, 0, 100), 2)          （0.8 / 0.2 可在设置里改）
"""

from __future__ import annotations

import math

import numpy as np

from backend.app.algorithms.features import GROUPS, dimension_weights, feature_groups
from backend.app.core.constants import BAND_HIGH, BAND_LOW, BAND_MID, BAND_WEAK, COSINE_WEIGHT, EUCLIDEAN_WEIGHT

_EPS = 1e-9
BAND_NAMES = ("高度", "较高", "一般", "低", "弱")


def describe_band(score: float, thresholds: tuple[float, float, float] | None = None) -> str:
    """≥90 高度 / ≥80 较高 / ≥70 一般 / ≥60 低 / 其余弱（60 写死）。"""
    high, mid, low = thresholds or (BAND_HIGH, BAND_MID, BAND_LOW)
    for floor, name in zip((high, mid, low, BAND_WEAK), BAND_NAMES):
        if score >= floor:
            return name
    return BAND_NAMES[-1]


def band_label(score: float, thresholds=None) -> str:
    return describe_band(score, thresholds) + "相似"


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


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def decay_score(ratio: float) -> float:
    """欧氏分衰减：d/scale → 100·exp(−(d/scale)²)。"""
    return 100.0 * math.exp(-(ratio**2))


def fit_robust_scaler(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    matrix = np.asarray(samples, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise ValueError("样本矩阵是空的")
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            center = np.nanmedian(matrix, axis=0)
            q75, q25 = np.nanpercentile(matrix, [75, 25], axis=0)
    center = np.where(np.isnan(center), 0.0, center)
    scale = q75 - q25
    scale = np.where(np.isnan(scale) | (scale <= _EPS), 1.0, scale)
    return center, scale


def _pairwise(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aa = np.sum(a * a, axis=1)[:, None]
    bb = np.sum(b * b, axis=1)[None, :]
    return np.sqrt(np.maximum(aa + bb - 2.0 * (a @ b.T), 0.0))


def median_pairwise_distance(z: np.ndarray) -> float:
    n = z.shape[0]
    if n < 2:
        return 1.0
    dist = _pairwise(z, z)
    upper = dist[np.triu_indices(n, k=1)]
    value = float(np.median(upper)) if upper.size else 1.0
    return value if value > _EPS else 1.0


class SimilarityEngine:
    """在样本池上拟合（中位数 / IQR / 距离尺度），然后给任意目标打分。"""

    def __init__(self, euclidean_weight: float = EUCLIDEAN_WEIGHT, cosine_weight: float = COSINE_WEIGHT, weights=None):
        self.euclidean_weight = float(euclidean_weight)
        self.cosine_weight = float(cosine_weight)
        w = np.asarray(weights if weights is not None else dimension_weights(), dtype=np.float64)
        self.weights = w / w.sum()
        self.root = np.sqrt(self.weights)
        self.center: np.ndarray | None = None
        self.scale: np.ndarray | None = None
        self.distance_scale = 1.0
        self.samples_z: np.ndarray | None = None

    def standardize(self, matrix: np.ndarray) -> np.ndarray:
        assert self.center is not None and self.scale is not None
        z = (np.asarray(matrix, dtype=np.float64) - self.center) / self.scale
        z = np.where(np.isnan(z), 0.0, z)  # 缺数 = 池中位数，标准化后为 0
        return z * self.root

    def fit(self, samples: np.ndarray) -> "SimilarityEngine":
        matrix = np.asarray(samples, dtype=np.float64)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        self.center, self.scale = fit_robust_scaler(matrix)
        self.samples_z = self.standardize(matrix)
        self.distance_scale = median_pairwise_distance(self.samples_z)
        return self

    def score_z(self, targets_z: np.ndarray, samples_z: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        sz = self.samples_z if samples_z is None else samples_z
        dist = _pairwise(targets_z, sz)
        euclid = 100.0 * np.exp(-((dist / self.distance_scale) ** 2))
        tn = np.linalg.norm(targets_z, axis=1)[:, None]
        sn = np.linalg.norm(sz, axis=1)[None, :]
        denom = tn * sn
        cos = np.zeros_like(dist)
        np.divide(targets_z @ sz.T, denom, out=cos, where=(tn > _EPS) & (sn > _EPS))
        cos = np.clip(cos, -1.0, 1.0)
        cosine = (cos + 1.0) / 2.0 * 100.0
        total = np.round(np.clip(self.euclidean_weight * euclid + self.cosine_weight * cosine, 0.0, 100.0), 2)
        return total, euclid, cosine

    def score(self, targets: np.ndarray) -> np.ndarray:
        t = np.asarray(targets, dtype=np.float64)
        if t.ndim == 1:
            t = t.reshape(1, -1)
        return self.score_z(self.standardize(t))[0]

    def group_scores(self, target: np.ndarray, sample: np.ndarray) -> dict[str, float]:
        """展示用：七组各自的欧氏分（同一标准化、同一距离尺度按组权重开方缩放）。"""
        tz = self.standardize(np.asarray(target).reshape(1, -1))[0]
        sz = self.standardize(np.asarray(sample).reshape(1, -1))[0]
        labels = np.asarray(feature_groups())
        out: dict[str, float] = {}
        from backend.app.core.constants import GROUP_WEIGHTS

        for group in GROUPS:
            idx = labels == group
            d = float(np.linalg.norm(tz[idx] - sz[idx])) / math.sqrt(GROUP_WEIGHTS[group])
            out[group] = round(decay_score(d / self.distance_scale), 2)
        return out


def score_matrix(targets: np.ndarray, samples: np.ndarray, **kwargs) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """便捷函数：在 samples 上拟合，再给 targets 打分。返回 (总分, 欧氏分, 余弦分)。"""
    engine = SimilarityEngine(**kwargs).fit(samples)
    t = np.asarray(targets, dtype=np.float64)
    if t.ndim == 1:
        t = t.reshape(1, -1)
    return engine.score_z(engine.standardize(t))


def score_pair(target, sample) -> float:
    return float(score_matrix(np.asarray(target), np.asarray(sample))[0][0, 0])


def summarize(row_scores: np.ndarray, threshold_high: float = BAND_HIGH) -> dict:
    """一只候选对样本池（已去掉自己）的四个数。"""
    if row_scores.size == 0:
        return {"sample_count": 0, "max_similarity": None, "avg_similarity": None, "high_similarity_count": 0, "band": "", "best": None}
    best = int(np.argmax(row_scores))
    return {
        "sample_count": int(row_scores.size),
        "max_similarity": round(float(row_scores[best]), 2),
        "avg_similarity": round(float(row_scores.mean()), 2),
        "high_similarity_count": int((row_scores >= threshold_high).sum()),
        "band": describe_band(float(row_scores[best])),
        "best": best,
    }
