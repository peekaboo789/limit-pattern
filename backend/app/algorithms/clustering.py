"""形态聚类：把样本池里已落库的涨停样本按三日形态分成几组。

三条硬约束，动一条就要同时动测试：

1. **聚类对象只是已确认涨停并落库的样本**。聚类是对已发生事实的事后归纳，
   不预测任何股票的涨跌，也不产出任何「涨停概率」。
2. **组名由组内样本的真实特征取中位数自动得到**（固定档位词表，每一档都是一个可核对的阈值），
   不是人工标签，也不是质心反读——质心在标准化空间里，没有可读单位。
   同名先补振幅档，仍同名才加序号。
3. **聚类在与相似度打分同一个加权空间里做**（对样本池拟合的 RobustScaler + 权重开方），
   否则聚出来的「组」和打分用的「距离」讲不到一起去。

PCA 二维投影只服务散点图。投影必然损失信息，图上「看着近」不等于形态近，
任何分数都不从它来。

sklearn 到真要算的时候才导入（连同 scipy 约一秒）：打分、扫描、实时监控都不碰它，
只有打开聚类页才会用到 KMeans / PCA / 轮廓系数，所以启动路径上不出现 sklearn。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from typing import TYPE_CHECKING, NamedTuple

import numpy as np

from backend.app.algorithms.features import dimension_weights
from backend.app.algorithms.similarity import fit_robust_scaler
from backend.app.core.constants import CLUSTER_RANDOM_STATE

if TYPE_CHECKING:  # pragma: no cover - 只给静态检查看，运行期一律不在模块顶层导入 sklearn
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA

#: 轮廓系数是 O(n²)：样本池一般几十到两百条，直接算没问题；
#: 超过这个量级就等间隔抽样子集再评估，避免一次吃掉几百 MB。
MAX_SILHOUETTE_SAMPLES = 300

#: 组内距离为 0（样本完全相同的退化情形）时用它表示「完全可分」，
#: 避免除零产生 inf——inf 进不了 JSON，前端 `response.json()` 会当场失败。
MAX_SEPARATION = 9999.0

CLUSTER_DISCLAIMER = (
    "形态组是对样本池里已涨停样本的事后形态归纳，组名由组内样本的真实特征自动生成；"
    "分组与相似度都只描述历史形态接近程度，不构成任何涨跌判断或投资建议。"
)


# --- sklearn 延迟导入 ------------------------------------------------------


class SklearnTools(NamedTuple):
    kmeans_cls: type["KMeans"]
    pca_cls: type["PCA"]
    silhouette: object


@cache
def load_sklearn() -> SklearnTools:
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.metrics import silhouette_score

    return SklearnTools(KMeans, PCA, silhouette_score)


# --- 形态画像 -------------------------------------------------------------


@dataclass(frozen=True)
class ClusterSignature:
    """一组样本的真实形态画像。

    单位与特征表一致：`return_3d`/`slope_close`/`amplitude_*` 是百分数，
    `volume_trend` 是倍数（T-1 的量 ÷ 前两天的均量），`position_20` 是 0~1。
    `turnover_rate` 可以是 None：公开源那一路整列换手率都缺，缺就写「没数」，
    不能让它在中位数里变成 0.00%——那是把「不知道」说成一个具体的数。
    """

    return_3d: float
    slope_close: float
    volatility_3d: float
    volume_trend: float
    amplitude_sum: float
    amplitude_mean: float
    turnover_rate: float | None
    position_20: float

    def describe(self) -> str:
        turnover = "—" if self.turnover_rate is None else f"{self.turnover_rate:.2f}%"
        return (
            f"三日累计 {self.return_3d:+.1f}%，"
            f"量能 {self.volume_trend:.2f}×，"
            f"三日振幅 {self.amplitude_sum:.1f}%，"
            f"日均振幅 {self.amplitude_mean:.1f}%，"
            f"T-1 换手 {turnover}，"
            f"T-1 位于 20 日区间 {self.position_20 * 100:.0f}%"
        )

    def as_dict(self) -> dict[str, float | None]:
        payload: dict[str, float | None] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            payload[name] = None if value is None else round(float(value), 4)
        return payload


def _number(value) -> float | None:
    """把库里那一格折成有限浮点数；缺的、坏的一律 None，交给中位数去滤。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _median(values: Sequence[float | None], default: float = 0.0) -> float:
    present = _present(values)
    return float(statistics.median(present)) if present else default


def _present(values: Sequence[float | None]) -> list[float]:
    return [float(value) for value in values if _number(value) is not None]


def _feature(day: dict, name: str) -> float | None:
    """`day_details` 里那一天的形态特征值。样本那天缺列就是缺列，不补 0。"""
    return _number((day.get("features") or {}).get(name))


def signature_from_payloads(payloads: Sequence[dict]) -> ClusterSignature:
    """从组内样本落库的真实特征取中位数，作为这一组的形态画像。

    入参是 `cluster_service` 从 `limit_up_patterns` 拼出来的两条：
    `aggregates`（向量尾部那七项）与 `days`（`day_details`，顺序是 T-3、T-2、T-1，
    所以最后一天才是 T-1）。涨停日那一行不在里面——它只贡献标签。
    """
    aggregates = [item.get("aggregates") or {} for item in payloads]
    day_rows = [item.get("days") or [] for item in payloads]
    amplitudes: list[float] = []
    for days in day_rows:
        present = [_number((day.get("features") or {}).get("amplitude")) for day in days]
        usable = [value for value in present if value is not None]
        if usable:
            amplitudes.append(sum(usable) / len(usable))
    last = [days[-1] if days else {} for days in day_rows]
    return ClusterSignature(
        return_3d=_median([item.get("return_3d") for item in aggregates]),
        slope_close=_median([item.get("slope_close") for item in aggregates]),
        volatility_3d=_median([item.get("volatility_3d") for item in aggregates]),
        volume_trend=_median([item.get("volume_trend") for item in aggregates]),
        amplitude_sum=_median([item.get("amplitude_sum") for item in aggregates]),
        amplitude_mean=_median(amplitudes),
        turnover_rate=_optional_median([_feature(day, "turnover_rate") for day in last]),
        position_20=_median([_feature(day, "position_20") for day in last]),
    )


def _optional_median(values: Sequence[float | None]) -> float | None:
    """整组都缺这一列时回 None，而不是 0：缺列与「换手真的是 0%」是两件事。"""
    present = _present(values)
    return float(statistics.median(present)) if present else None


# --- 自动命名 -------------------------------------------------------------

#: 档位词表：每一档都是一个可核对的阈值，改阈值必须同步改测试。
#: 量能那一档吃的是倍数（1.0 表示 T-1 的量与前两天均量齐平），不是百分数。
RETURN_WORDS: tuple[tuple[float, str], ...] = (
    (12.0, "加速拉升"),
    (6.0, "强势上行"),
    (2.0, "温和上行"),
    (-2.0, "横向整理"),
    (-6.0, "小幅回落"),
)

VOLUME_WORDS: tuple[tuple[float, str], ...] = (
    (1.80, "急剧放量"),
    (1.15, "持续放量"),
    (0.85, "量能平稳"),
)

POSITION_WORDS: tuple[tuple[float, str], ...] = (
    (0.85, "高位区"),
    (0.60, "中高位"),
    (0.40, "中位区"),
)

AMPLITUDE_WORDS: tuple[tuple[float, str], ...] = (
    (12.0, "大幅震荡"),
    (6.0, "振幅适中"),
)


def _pick_word(value: float, table: Sequence[tuple[float, str]], fallback: str) -> str:
    for threshold, word in table:
        if value >= threshold:
            return word
    return fallback


def momentum_word(return_3d: float) -> str:
    return _pick_word(return_3d, RETURN_WORDS, "深度回调")


def volume_word(volume_trend: float) -> str:
    return _pick_word(volume_trend, VOLUME_WORDS, "缩量整理")


def position_word(position_20: float) -> str:
    return _pick_word(position_20, POSITION_WORDS, "低位区")


def amplitude_word(amplitude_sum: float) -> str:
    return _pick_word(amplitude_sum, AMPLITUDE_WORDS, "窄幅波动")


def base_name(signature: ClusterSignature) -> str:
    return (
        f"{momentum_word(signature.return_3d)}"
        f"·{volume_word(signature.volume_trend)}"
        f"·{position_word(signature.position_20)}"
    )


def disambiguate(base_names: Sequence[str], signatures: Sequence[ClusterSignature]) -> list[str]:
    """同名逐级加词消歧：先补振幅档，仍同名才加序号。

    序号是最后手段——它不带任何形态信息，只在样本高度同质时才会出现。
    """
    names = list(base_names)
    counters: dict[str, int] = {}
    for name in names:
        counters[name] = counters.get(name, 0) + 1
    duplicates = {name for name, count in counters.items() if count > 1}
    for index, name in enumerate(names):
        if name in duplicates:
            names[index] = f"{name}·{amplitude_word(signatures[index].amplitude_sum)}"

    counters = {}
    for name in names:
        counters[name] = counters.get(name, 0) + 1
    seen: dict[str, int] = {}
    for index, name in enumerate(names):
        if counters[name] > 1:
            seen[name] = seen.get(name, 0) + 1
            names[index] = f"{name} #{seen[name]}"
    return names


def name_clusters(signatures: Sequence[ClusterSignature]) -> list[str]:
    return disambiguate([base_name(item) for item in signatures], signatures)


# --- 聚类 -----------------------------------------------------------------


@dataclass(frozen=True)
class ShapeCluster:
    cluster_id: int
    name: str
    size: int
    share: float
    member_indices: tuple[int, ...]
    centroid: np.ndarray
    signature: ClusterSignature
    representative_index: int
    intra_distance: float
    nearest_other_distance: float
    description: str = ""

    @property
    def separation(self) -> float:
        """组间最近距离 ÷ 组内平均距离：大于 1 说明各组分得开。"""
        if self.intra_distance <= 1e-9:
            return 0.0 if self.nearest_other_distance <= 1e-9 else MAX_SEPARATION
        return round(min(self.nearest_other_distance / self.intra_distance, MAX_SEPARATION), 4)


@dataclass(frozen=True)
class ClusterPlan:
    k: int
    selected_by: str
    silhouette: float
    evaluated: list[dict] = field(default_factory=list)


def weighted_space(matrix: np.ndarray) -> np.ndarray:
    """把原始向量搬到与打分同一个空间：对这批样本拟合 RobustScaler，再按权重开方。

    `similarity.score_matrix` 也是先标准化（分母由**样本池**拟合）再乘权重开方，
    所以这里出来的距离与榜上那个分数说的是同一件事。权重按 `dimension_weights()`
    归一到和为 1，与打分那边一个口径。
    """
    values = np.asarray(matrix, dtype=np.float64)
    if values.ndim == 1:
        values = values.reshape(1, -1)
    median, iqr = fit_robust_scaler(values)
    weights = np.asarray(dimension_weights(), dtype=np.float64)
    weights = weights / float(weights.sum())
    return (values - median) / iqr * np.sqrt(weights)


def kmeans(
    matrix: np.ndarray,
    k: int,
    *,
    random_state: int = CLUSTER_RANDOM_STATE,
    n_init: int = 10,
    max_iter: int = 300,
) -> tuple[np.ndarray, np.ndarray]:
    """对加权空间里的样本做 KMeans，返回 (标签, 质心)。"""
    values = np.asarray(matrix, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError("聚类输入必须是二维数组")
    if k < 1:
        raise ValueError("k 至少为 1")
    if values.shape[0] < k:
        raise ValueError(f"样本数 {values.shape[0]} 少于分组数 {k}，无法聚类")
    model = load_sklearn().kmeans_cls(
        n_clusters=k, n_init=n_init, max_iter=max_iter, random_state=random_state
    )
    labels = model.fit_predict(values)
    return np.asarray(labels, dtype=np.int64), model.cluster_centers_


def _subsample(matrix: np.ndarray, limit: int) -> np.ndarray:
    if matrix.shape[0] <= limit:
        return matrix
    step = math.ceil(matrix.shape[0] / limit)
    return matrix[::step]


def select_k(
    matrix: np.ndarray,
    k_values: Sequence[int],
    *,
    random_state: int = CLUSTER_RANDOM_STATE,
) -> ClusterPlan:
    """按轮廓系数挑分组数；并列时取较小的 k（更少的主观分组）。"""
    values = np.asarray(matrix, dtype=np.float64)
    total = int(values.shape[0])
    candidates = sorted({int(k) for k in k_values if 2 <= int(k) <= total - 1})
    if not candidates:
        return ClusterPlan(
            k=1 if total else 0,
            selected_by="样本不足，不分组",
            silhouette=0.0,
            evaluated=[],
        )
    working = _subsample(values, MAX_SILHOUETTE_SAMPLES)
    silhouette = load_sklearn().silhouette
    evaluated: list[dict] = []
    best_k = candidates[0]
    best_score = -1.0
    for k in candidates:
        labels, _ = kmeans(working, k, random_state=random_state)
        score = float(silhouette(working, labels)) if len(set(labels.tolist())) > 1 else 0.0
        evaluated.append({"k": k, "silhouette": round(score, 4)})
        if score > best_score + 1e-12:
            best_k, best_score = k, score
    return ClusterPlan(
        k=best_k,
        selected_by="轮廓系数最大",
        silhouette=round(best_score, 4),
        evaluated=evaluated,
    )


def build_clusters(
    matrix: np.ndarray,
    labels: np.ndarray,
    centroids: np.ndarray,
    payloads: Sequence[dict],
) -> list[ShapeCluster]:
    """把 KMeans 的结果整理成带名称与画像的形态组，按规模从大到小排。"""
    values = np.asarray(matrix, dtype=np.float64)
    total = int(values.shape[0])
    grouped: dict[int, list[int]] = {}
    for index, label in enumerate(np.asarray(labels, dtype=np.int64).tolist()):
        grouped.setdefault(int(label), []).append(index)

    clusters: list[ShapeCluster] = []
    for label in sorted(grouped, key=lambda item: (-len(grouped[item]), item)):
        members = grouped[label]
        centroid = np.asarray(centroids[label], dtype=np.float64)
        distances = np.linalg.norm(values[members] - centroid, axis=1)
        other = np.delete(np.asarray(centroids, dtype=np.float64), label, axis=0)
        nearest_other = (
            float(np.min(np.linalg.norm(other - centroid, axis=1))) if other.shape[0] else 0.0
        )
        clusters.append(
            ShapeCluster(
                cluster_id=int(label),
                name="",
                size=len(members),
                share=round(len(members) / total, 4) if total else 0.0,
                member_indices=tuple(members),
                centroid=centroid,
                signature=signature_from_payloads([payloads[index] for index in members]),
                representative_index=members[int(np.argmin(distances))],
                intra_distance=round(float(np.mean(distances)), 4),
                nearest_other_distance=round(nearest_other, 4),
            )
        )
    names = name_clusters([item.signature for item in clusters])
    # 画像那一列直接来自组内中位数，与 `signature` 是同一份数：页面既能读句子也能读格子。
    return [
        replace(item, name=name, description=item.signature.describe())
        for item, name in zip(clusters, names)
    ]


def fit_projection(matrix: np.ndarray) -> "PCA":
    """拟合 PCA 二维投影。质心要用同一个模型变换，才与样本同一坐标系。"""
    values = np.asarray(matrix, dtype=np.float64)
    components = min(2, values.shape[0], values.shape[1])
    if components < 2:
        raise ValueError("样本不足以做二维投影")
    return load_sklearn().pca_cls(n_components=components).fit(values)


def apply_projection(model: "PCA", matrix: np.ndarray) -> np.ndarray:
    return model.transform(np.asarray(matrix, dtype=np.float64))
