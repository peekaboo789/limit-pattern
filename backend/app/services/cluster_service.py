"""聚类分析：只用最近 3 个交易日涨停样本的涨停前三日形态向量（与相似度同一套特征），不看未来。

scikit-learn 是可选依赖。没装时抛 ClusterUnavailable，接口回 503。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime

import numpy as np

from backend.app.algorithms.clustering import ClusterUnavailable, MISSING_MESSAGE, cluster_vectors
from backend.app.algorithms.features import feature_names
from backend.app.core.clock import SHANGHAI
from backend.app.core.config import data_dir

INDEX = {name: i for i, name in enumerate(feature_names())}
CLOSE_IDX = INDEX["return_3d"]
VOLUME_IDX = INDEX["t1_volume_ratio_5"]
POSITION_IDX = INDEX["t1_position_20"]
# 中心形态小图：三天收盘涨跌和量比（t3 最早，t1 是涨停前一天）
PATTERN_FIELDS = ("close_change", "volume_ratio_5", "amplitude")

__all__ = ["ClusterUnavailable", "MISSING_MESSAGE", "build_dataset", "run_on_dataset", "run_cluster", "get_cluster"]


def clusters_dir():
    path = data_dir() / "clusters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_dataset(session, day: date) -> dict:
    from backend.app.services.similarity_service import _names, _samples

    dates, samples, skipped = _samples(session, day)
    names = _names(session)
    return {
        "format": "limit-pattern-features/1",
        "date": day.isoformat(),
        "pool_dates": [d.isoformat() for d in dates],
        "feature_names": feature_names(),
        "note": "最近 3 个交易日涨停样本，向量取涨停前三根 K 线，不含涨停当天，不含未来",
        "samples": [
            {
                "code": s["code"],
                "name": names.get(s["code"], ""),
                "limit_date": s["limit_date"].isoformat(),
                "window": s["window"],
                "vector": [round(float(v), 6) for v in s["vector"]],
            }
            for s in samples
        ],
        "skipped": skipped,
    }


def dataset_to_csv(dataset: dict) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["code", "name", "limit_date", *dataset["feature_names"]])
    for s in dataset["samples"]:
        writer.writerow([s["code"], s["name"], s["limit_date"], *s["vector"]])
    return out.getvalue()


def dataset_from_csv(text: str, day: str = "") -> dict:
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    names = header[3:]
    samples = []
    for row in reader:
        if not row:
            continue
        samples.append({"code": row[0], "name": row[1], "limit_date": row[2], "window": [], "vector": [float(v) for v in row[3:]]})
    return {"format": "limit-pattern-features/1", "date": day, "pool_dates": [], "feature_names": names, "samples": samples}


def _pattern(center: list[float], names: list[str]) -> list[dict]:
    index = {name: i for i, name in enumerate(names)}
    rows = []
    for offset in ("t3", "t2", "t1"):
        row = {"day": offset}
        for field in PATTERN_FIELDS:
            i = index.get(f"{offset}_{field}")
            row[field] = None if i is None else round(float(center[i]), 4)
        rows.append(row)
    return rows


def run_on_dataset(dataset: dict, k: int | None = None, method: str = "kmeans") -> dict:
    if method not in ("kmeans", "agglomerative"):
        raise ValueError("method 只能是 kmeans 或 agglomerative")
    samples = dataset.get("samples") or []
    names = dataset.get("feature_names") or feature_names()
    index = {name: i for i, name in enumerate(names)}
    base = {
        "date": dataset.get("date"),
        "pool_dates": dataset.get("pool_dates") or [],
        "method": method,
        "requested_k": k or "auto",
        "sample_count": len(samples),
        "disclaimer": "聚类只描述形态分组，不是涨停概率",
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "clusters": [],
        "points": [],
    }
    if len(samples) < 3:
        base["note"] = "样本不足（至少 3 个），聚不了类"
        return base
    matrix = np.asarray([s["vector"] for s in samples], dtype=float)
    result = cluster_vectors(
        matrix,
        k=k,
        method=method,
        close_index=index.get("return_3d", CLOSE_IDX),
        volume_index=index.get("t1_volume_ratio_5", VOLUME_IDX),
        position_index=index.get("t1_position_20", POSITION_IDX),
    )
    labels = result["labels"]
    base["k"] = result["k"]
    base["silhouette"] = result.get("silhouette")
    centers = result.get("centers") or []
    for cid in range(result["k"]):
        members = [
            {"code": s["code"], "name": s.get("name", ""), "limit_date": s.get("limit_date")}
            for s, label in zip(samples, labels)
            if label == cid
        ]
        center = centers[cid] if cid < len(centers) else []
        base["clusters"].append(
            {
                "id": cid,
                "name": result["names"][cid] if cid < len(result["names"]) else f"组{cid + 1}",
                "size": len(members),
                "members": members,
                "pattern": _pattern(center, names) if center else [],
                "return_3d": round(float(center[index["return_3d"]]), 3) if center and "return_3d" in index else None,
            }
        )
    for s, label, point in zip(samples, labels, result.get("points") or []):
        base["points"].append({"code": s["code"], "name": s.get("name", ""), "cluster": int(label), "x": round(point[0], 4), "y": round(point[1], 4)})
    return base


def run_cluster(session, day: date, k: int | None = None, method: str = "kmeans") -> dict:
    result = run_on_dataset(build_dataset(session, day), k, method)
    path = clusters_dir() / f"{day.isoformat()}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def get_cluster(day: date | None = None) -> dict | None:
    files = sorted(clusters_dir().glob("*.json"))
    if day is not None:
        files = [f for f in files if f.stem == day.isoformat()]
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))
