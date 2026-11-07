"""形态聚类。sklearn 第一次真要算的时候才导入，不挡启动。"""

from __future__ import annotations

from functools import cache

import numpy as np


class ClusterUnavailable(RuntimeError):
    """没装 scikit-learn。"""


MISSING_MESSAGE = "聚类需要本地安装 scikit-learn：pip install -r requirements-cluster.txt"


@cache
def _load():
    from sklearn.cluster import AgglomerativeClustering, KMeans
    from sklearn.decomposition import PCA
    from sklearn.metrics import silhouette_score

    return KMeans, silhouette_score, PCA, AgglomerativeClustering


def load_sklearn():
    try:
        kmeans, silhouette, pca, _agg = _load()
    except ImportError as exc:
        raise ClusterUnavailable(MISSING_MESSAGE) from exc
    return kmeans, silhouette, pca


def sklearn_available() -> bool:
    try:
        _load()
    except ImportError:
        return False
    return True


def standardize(matrix: np.ndarray) -> np.ndarray:
    mean = matrix.mean(axis=0)
    std = matrix.std(axis=0)
    std[std < 1e-9] = 1.0
    return (matrix - mean) / std


def _name_cluster(mean: np.ndarray, close_index: int, volume_index: int, position_index: int) -> str:
    close_change = float(mean[close_index])
    volume_ratio = float(mean[volume_index])
    position = float(mean[position_index])
    if close_change >= 3 and volume_ratio >= 1.4:
        return "放量上攻"
    if close_change <= -1:
        return "调整回落"
    if position >= 0.75 and abs(close_change) < 3:
        return "高位整理"
    if volume_ratio <= 0.8 and abs(close_change) < 2:
        return "缩量整理"
    if close_change >= 1:
        return "温和上涨"
    return "横盘震荡"


def cluster_vectors(
    vectors: np.ndarray,
    *,
    k: int | None = None,
    close_index: int = 3,
    volume_index: int = 9,
    position_index: int = 18,
    random_state: int = 0,
    method: str = "kmeans",
) -> dict:
    """k 不指定时在 2 到 6 里挑轮廓系数最高的。样本太少就如实说。"""
    matrix = np.asarray(vectors, dtype=np.float64)
    count = int(matrix.shape[0])
    if count < 2:
        return {"k": 0, "labels": [], "names": [], "silhouette": None, "note": "样本不足，聚不了类"}
    kmeans_cls, silhouette_score, pca_cls = load_sklearn()
    agg_cls = _load()[3]
    raw = matrix
    matrix = standardize(matrix)
    upper = min(6, count - 1)
    if k is None:
        candidates = range(2, upper + 1)
    else:
        chosen = max(2, min(int(k), upper))
        candidates = range(chosen, chosen + 1)
    best = None
    for candidate in candidates:
        if method == "agglomerative":
            labels = agg_cls(n_clusters=candidate, linkage="ward").fit_predict(matrix)
        else:
            labels = kmeans_cls(n_clusters=candidate, n_init=10, random_state=random_state).fit_predict(matrix)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(matrix, labels))
        if best is None or score > best["silhouette"]:
            centers = np.asarray([raw[labels == i].mean(axis=0) for i in range(candidate)])
            best = {"k": candidate, "labels": labels, "silhouette": score, "centers": centers}
    if best is None:
        return {"k": 1, "labels": [0] * count, "names": ["单一形态"], "silhouette": None, "points": []}
    labels = best["labels"]
    names: list[str] = []
    used: dict[str, int] = {}
    for center in best["centers"]:
        name = _name_cluster(center, close_index, volume_index, position_index)
        used[name] = used.get(name, 0) + 1
        names.append(name if used[name] == 1 else f"{name}{used[name]}")
    points = []
    if matrix.shape[1] >= 2 and count >= 2:
        pca = pca_cls(n_components=2, random_state=random_state)
        projected = pca.fit_transform(matrix)
        points = projected.tolist()
    return {
        "k": int(best["k"]),
        "labels": [int(item) for item in labels],
        "names": names,
        "silhouette": None if best["silhouette"] is None else round(float(best["silhouette"]), 4),
        "points": points,
        "centers": [center.tolist() for center in best["centers"]],
        "method": method,
    }
