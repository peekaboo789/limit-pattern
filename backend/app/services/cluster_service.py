"""聚类分析：只用近 N 个交易日（默认 30）涨停样本的涨停前三日形态向量（与相似度同一套特征），不看未来。

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
CLOSE_IDX = INDEX["three_day_return"]
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
    from backend.app.services.history_backfill import bulk_bars
    from backend.app.services.similarity_service import _names, _samples

    with bulk_bars(session, day):
        dates, samples, skipped = _samples(session, day)
    names = _names(session)
    return {
        "format": "limit-pattern-features/1",
        "date": day.isoformat(),
        "pool_dates": [d.isoformat() for d in dates],
        "feature_names": feature_names(),
        "note": "近 N 个交易日涨停样本（默认 30，设置可改），向量取涨停前三根 K 线，不含涨停当天，不含未来",
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
    matrix = np.asarray([[np.nan if v is None else v for v in s["vector"]] for s in samples], dtype=float)
    if np.isnan(matrix).any():  # 缺失特征按样本池中位数补（与相似度同口径）
        medians = np.nan_to_num(np.nanmedian(np.where(np.isnan(matrix).all(axis=0), 0.0, matrix), axis=0))
        matrix = np.where(np.isnan(matrix), medians, matrix)
    result = cluster_vectors(
        matrix,
        k=k,
        method=method,
        close_index=index.get("three_day_return", CLOSE_IDX),
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
                "return_3d": round(float(center[index["three_day_return"]]), 3) if center and "three_day_return" in index else None,
            }
        )
    for s, label, point in zip(samples, labels, result.get("points") or []):
        base["points"].append({"code": s["code"], "name": s.get("name", ""), "cluster": int(label), "x": round(point[0], 4), "y": round(point[1], 4)})
    return base


INTERPRET_SYSTEM = (
    "你是资深 A 股涨停板研究分析师，熟悉首板、连板、缩量洗盘、放量突破等形态。下面是若干聚类组的中心形态数字（涨停前第 3、2、1 天的涨跌、振幅、量比等）。"
    "为每组写一句不超过 40 字的中文解读，只描述数字体现的形态（如缩量横盘、放量拉升），"
    "不得预测涨跌、不得给出买卖建议、不得编造新闻。"
    '只返回 JSON：{"items":[{"id":0,"text":"..."}]}'
)


def interpret_clusters(result: dict, chat=None) -> dict:
    """可选：让 AI 按中心形态数字写一句解读，存到 ai_text；失败或未配置时保留规则标签。"""
    from backend.app.services.summary_service import AiFailed, AiNotConfigured, _extract_json, ai_ready, call_chat

    clusters = result.get("clusters") or []
    if not clusters:
        return result
    if chat is None and not ai_ready():
        result["ai_note"] = "未配置 AI 接口，只显示规则标签"
        return result
    lines = []
    for c in clusters:
        rows = "; ".join(
            f"{row['day']}: " + ", ".join(f"{k}={v}" for k, v in row.items() if k != "day" and v is not None)
            for row in c.get("pattern") or []
        )
        lines.append(f"id={c['id']} 规则标签={c['name']} 样本数={c['size']} 三日累计涨跌={c.get('return_3d')} | {rows}")
    prompt = "\n".join(lines)
    try:
        text = chat(prompt) if chat is not None else call_chat(prompt, system=INTERPRET_SYSTEM)
        payload = _extract_json(text) or {}
        by_id = {int(item.get("id")): str(item.get("text") or "").strip()[:80] for item in payload.get("items") or [] if "id" in item}
    except (AiFailed, AiNotConfigured, ValueError, TypeError) as exc:
        result["ai_note"] = f"AI 解读没写成，保留规则标签：{exc}"[:300]
        return result
    for c in clusters:
        if by_id.get(c["id"]):
            c["ai_text"] = by_id[c["id"]]
    result["ai_note"] = "AI 解读只根据中心形态数字，不是预测"
    return result


def run_cluster(session, day: date, k: int | None = None, method: str = "kmeans", interpret: bool = False) -> dict:
    result = run_on_dataset(build_dataset(session, day), k, method)
    if interpret:
        interpret_clusters(result)
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
