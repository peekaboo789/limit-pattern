"""形态聚类服务：分组与归属都只吃库里已落库的涨停样本。

分组对象是 `pattern_service.pool_patterns`（样本池这几天库里的那几条样本），
不是现算的涨停池。差这一句看上去只是取数口径，实际决定了这一页能不能被核对：
页面上每一个形态组都要能指回样本库里具体的几行，否则用户重建样本之后组变了，
等于之前那份分组描述的是一个不存在的样本集合。

归属（`assignment`）用与首页榜、盘中榜、回测、匹配明细**同一把尺子**打分：
`similarity.score_components`。这一页不产生第二套分数，也不产生任何概率。
"""

from __future__ import annotations

from datetime import date

import numpy as np
from sqlalchemy.orm import Session

from backend.app.algorithms.clustering import (
    CLUSTER_DISCLAIMER,
    ClusterPlan,
    ShapeCluster,
    apply_projection,
    build_clusters,
    fit_projection,
    kmeans,
    load_sklearn,
    select_k,
    weighted_space,
)
from backend.app.algorithms.features import VECTOR_DIM, build_three_day_window
from backend.app.algorithms.similarity import score_components
from backend.app.core.constants import (
    BAND_HIGH,
    CLUSTER_MAX_K,
    CLUSTER_MIN_K,
    CLUSTER_MIN_SAMPLES,
)
from backend.app.core.logging import get_logger
from backend.app.models.tables import LimitUpPattern
from backend.app.services.history_backfill import load_bars
from backend.app.services.pattern_service import (
    aggregates_of,
    pool_pattern_dates,
    pool_patterns,
    stock_names,
    vector_of,
)

logger = get_logger("cluster_service")

#: 少于这个样本数不聚类：那一档只能得到「每组一个样本」的退化结果，没有归纳意义。
MIN_SAMPLES = CLUSTER_MIN_SAMPLES


class ClusterUnavailable(Exception):
    """样本池不足以聚类。API 转成 400，绝不返回编造的分组。"""


def pool_size_error(count: int, as_of: date) -> str:
    return (
        f"{as_of.isoformat()} 的样本池只有 {count} 条落库样本，"
        f"至少 {MIN_SAMPLES} 条才能做形态聚类。先在数据管理页重建涨停样本。"
    )


def _payload(row: LimitUpPattern) -> dict:
    """组画像要吃的那两条真数：向量尾部七项聚合 + 形态三天的单日特征。"""
    return {"aggregates": aggregates_of(row), "days": list(row.day_details or [])}


def _brief(row: LimitUpPattern, names: dict[str, str], **extra) -> dict:
    payload = {
        "code": row.stock_code,
        "name": names.get(row.stock_code, ""),
        "limit_date": row.limit_up_date.isoformat() if row.limit_up_date else "",
        "window": [str(item) for item in (row.window_dates or [])],
    }
    payload.update(extra)
    return payload


def _pool_rows(session: Session, as_of: date) -> tuple[list[LimitUpPattern], int]:
    """库里的样本池。返回 (可用样本行, 因向量长度对不上而被跳过的行数)。

    长度不对的行是旧口径写的，拿来聚类会把不同形状的向量当成同一件事，
    所以直接跳过并如实报数，不截断、不补零。
    """
    _dates, rows = pool_patterns(session, as_of)
    usable = [row for row in rows if len(vector_of(row)) == VECTOR_DIM]
    return usable, len(rows) - len(usable)


def _validate_k(k: int, sample_count: int) -> None:
    if k < CLUSTER_MIN_K:
        raise ClusterUnavailable(f"分组数至少为 {CLUSTER_MIN_K}")
    if k >= sample_count:
        raise ClusterUnavailable(
            f"分组数 {k} 不能大于等于样本数 {sample_count}，否则每组只剩一个样本"
        )


def _silhouette(matrix: np.ndarray, labels: np.ndarray) -> float:
    if len(set(np.asarray(labels).tolist())) < 2:
        return 0.0
    return round(float(load_sklearn().silhouette(matrix, labels)), 4)


def _fit(
    vectors: list[list[float]],
    payloads: list[dict],
    k: int | None,
) -> tuple[np.ndarray, ClusterPlan, np.ndarray, np.ndarray, list[ShapeCluster]]:
    """分组数（自动或由调用方指定）→ KMeans → 带名称与画像的形态组。

    自动那条路里 `select_k` 可能按规模抽样评估轮廓系数，最终拟合始终用全量样本矩阵：
    轮廓系数只是挑 k 的依据，分组结果不来自抽样的那份矩阵。
    """
    matrix = weighted_space(np.asarray(vectors, dtype=np.float64))
    if k is None:
        plan = select_k(matrix, range(CLUSTER_MIN_K, CLUSTER_MAX_K + 1))
        labels, centroids = kmeans(matrix, plan.k)
    else:
        _validate_k(int(k), int(matrix.shape[0]))
        labels, centroids = kmeans(matrix, int(k))
        plan = ClusterPlan(
            k=int(k),
            selected_by="调用方指定",
            silhouette=_silhouette(matrix, labels),
            evaluated=[],
        )
    return matrix, plan, labels, centroids, build_clusters(
        matrix, labels, centroids, payloads
    )


def _projection(
    matrix: np.ndarray,
    labels: np.ndarray,
    centroids: np.ndarray,
    rows: list[LimitUpPattern],
    clusters: list[ShapeCluster],
) -> list[dict]:
    """PCA 二维投影，只服务散点图；不参与任何打分与分组。

    质心那一行按 `cluster_id` 取：sklearn 的 `cluster_centers_` 第 i 行就是标签 i，
    而 `clusters` 是按规模排的。用 enumerate 的序号去对齐两者，一旦有空标签
    （k 挑得比实际分出来的组多）就会把 A 组的样本画到 B 组质心旁边。
    """
    try:
        model = fit_projection(matrix)
        sample_xy = apply_projection(model, matrix)
        centroid_xy = apply_projection(model, np.asarray(centroids, dtype=np.float64))
    except ValueError:
        return []
    names_of = {group.cluster_id: group.name for group in clusters}
    points = [
        {
            "x": round(float(sample_xy[index][0]), 4),
            "y": round(float(sample_xy[index][1]), 4),
            "cluster_id": int(labels[index]),
            "code": row.stock_code,
            "name": "",
            "is_centroid": False,
            "group": names_of.get(int(labels[index]), ""),
        }
        for index, row in enumerate(rows)
    ]
    points.extend(
        {
            "x": round(float(centroid_xy[group.cluster_id][0]), 4),
            "y": round(float(centroid_xy[group.cluster_id][1]), 4),
            "cluster_id": group.cluster_id,
            "code": "",
            "name": group.name,
            "is_centroid": True,
            "group": group.name,
        }
        for group in clusters
        if group.cluster_id < centroid_xy.shape[0]
    )
    return points


def cluster(
    session: Session,
    as_of: date,
    *,
    k: int | None = None,
    include_members: bool = True,
) -> dict:
    """样本池分成几组、每组长什么样、散点图怎么画。"""
    rows, ignored = _pool_rows(session, as_of)
    if len(rows) < MIN_SAMPLES:
        raise ClusterUnavailable(pool_size_error(len(rows), as_of))
    names = stock_names(session)
    matrix, plan, labels, centroids, clusters = _fit(
        [vector_of(row) for row in rows], [_payload(row) for row in rows], k
    )
    groups = [
        {
            "cluster_id": group.cluster_id,
            "name": group.name,
            "size": group.size,
            "share": group.share,
            "description": group.description,
            "intra_distance": group.intra_distance,
            "nearest_other_distance": group.nearest_other_distance,
            "separation": group.separation,
            "signature": group.signature.as_dict(),
            "representative": _brief(
                rows[group.representative_index], names, is_representative=True
            ),
            "members": (
                [
                    _brief(
                        rows[index],
                        names,
                        is_representative=index == group.representative_index,
                    )
                    for index in group.member_indices
                ]
                if include_members
                else []
            ),
        }
        for group in clusters
    ]
    logger.info(
        "形态聚类：日期=%s 样本=%d 分组=%d（%s，轮廓系数 %.4f）",
        as_of,
        len(rows),
        plan.k,
        plan.selected_by,
        plan.silhouette,
    )
    return {
        "trade_date": as_of.isoformat(),
        "pool_dates": [day.isoformat() for day in pool_pattern_dates(session, as_of)],
        "sample_count": len(rows),
        "ignored_stale": ignored,
        "k": plan.k,
        "selected_by": plan.selected_by,
        "silhouette": plan.silhouette,
        "evaluated": plan.evaluated,
        "clusters": groups,
        "points": _projection(matrix, labels, centroids, rows, clusters),
        "high_band": BAND_HIGH,
        "k_min": CLUSTER_MIN_K,
        "k_max": CLUSTER_MAX_K,
        "disclaimer": CLUSTER_DISCLAIMER,
    }


def assignment(
    session: Session,
    code: str,
    as_of: date,
    *,
    k: int | None = None,
    top_members: int = 5,
) -> dict:
    """这只股票的三日形态更像哪一组已涨停形态。

    目标自己当天涨停时也在样本池里，但它那条样本**不参与**与自己这一组的比对：
    留着就是拿自己跟自己打 100 分，然后稳稳落在自己那一组。
    """
    rows, ignored = _pool_rows(session, as_of)
    if len(rows) < MIN_SAMPLES:
        raise ClusterUnavailable(pool_size_error(len(rows), as_of))
    names = stock_names(session)
    _matrix, plan, _labels, _centroids, clusters = _fit(
        [vector_of(row) for row in rows], [_payload(row) for row in rows], k
    )
    base = {
        "trade_date": as_of.isoformat(),
        "pool_dates": [day.isoformat() for day in pool_pattern_dates(session, as_of)],
        "code": code,
        "name": names.get(code, ""),
        "sample_count": len(rows),
        "ignored_stale": ignored,
        "k": plan.k,
        "assigned_cluster_id": None,
        "assigned_cluster_name": "",
        "margin": 0.0,
        "groups": [],
        "window": [],
        "skipped_reason": None,
        "high_band": BAND_HIGH,
        "disclaimer": CLUSTER_DISCLAIMER,
    }
    window = build_three_day_window(
        load_bars(session, code, as_of), as_of, include_as_of_date=True
    )
    if not window.ok:
        base["skipped_reason"] = (
            f"截至 {as_of.isoformat()} 凑不齐三天行情，算不出这只股票的形态窗口"
        )
        return base
    base["window"] = [bar.trade_date.isoformat() for bar in window.days]

    parts = score_components(
        np.asarray(window.vector, dtype=np.float64),
        np.asarray([vector_of(row) for row in rows], dtype=np.float64),
    )
    scores = parts["score"][0].astype(float).copy()
    self_mask = np.asarray([row.stock_code == code for row in rows], dtype=bool)

    groups: list[dict] = []
    for group in clusters:
        indexes = np.asarray(group.member_indices, dtype=np.int64)
        usable = indexes[~self_mask[indexes]]
        if usable.size == 0:
            continue
        group_scores = scores[usable]
        order = usable[np.argsort(-group_scores)[: max(1, int(top_members))]]
        groups.append(
            {
                "cluster_id": group.cluster_id,
                "name": group.name,
                "size": int(usable.size),
                "avg_similarity": round(float(group_scores.mean()), 2),
                "max_similarity": round(float(group_scores.max()), 2),
                "high_band_count": int((group_scores >= BAND_HIGH).sum()),
                "top_members": [
                    _brief(
                        rows[int(index)],
                        names,
                        similarity=round(float(scores[int(index)]), 2),
                    )
                    for index in order
                ],
            }
        )
    if not groups:
        base["skipped_reason"] = "样本池里除了这只股票自己没有别的样本，比无可比"
        return base

    groups.sort(key=lambda item: (-item["avg_similarity"], item["cluster_id"]))
    best = groups[0]
    base["assigned_cluster_id"] = best["cluster_id"]
    base["assigned_cluster_name"] = best["name"]
    base["margin"] = (
        round(best["avg_similarity"] - groups[1]["avg_similarity"], 2)
        if len(groups) > 1
        else round(best["avg_similarity"], 2)
    )
    base["groups"] = groups
    logger.info(
        "形态归属：%s 于 %s 归入「%s」（组内均分 %.2f，领先次优组 %.2f）",
        code,
        as_of,
        best["name"],
        best["avg_similarity"],
        base["margin"],
    )
    return base


__all__ = [
    "CLUSTER_DISCLAIMER",
    "ClusterUnavailable",
    "MIN_SAMPLES",
    "assignment",
    "cluster",
    "pool_size_error",
]
