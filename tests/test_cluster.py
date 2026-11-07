"""聚类：没装 scikit-learn 时给出明确提示；装了就跑真实聚类（缺依赖则跳过）。"""

import json

import pytest

from backend.app.algorithms import clustering
from tests.test_ranking_monitor import _seed_many


def _seed_limitups():
    from tests.test_api import _save, _series, weekdays

    days = weekdays(10)
    for index in range(8):
        limit_on = {7} if index % 2 else {8}
        _save(f"{600200 + index}", f"涨停{index}", "SSE", _series(days, limit_on=limit_on, start=6 + index))
    for index in range(6):
        _save(f"{2 + index:06d}", f"深{index}", "SZSE", _series(days, limit_on={9}, start=20 + index * 2))
    return days


def test_missing_sklearn_returns_503(client, monkeypatch):
    _seed_many()

    def boom():
        raise ImportError("no sklearn")

    monkeypatch.setattr(clustering, "_load", boom)
    response = client.post("/api/cluster/run", json={"k": "auto"})
    assert response.status_code == 503
    assert "聚类需要本地安装 scikit-learn" in response.json()["detail"]
    assert client.get("/api/cluster/status").json()["available"] is False


def test_feature_export_and_csv_round_trip(client):
    days = _seed_limitups()
    response = client.get("/api/cluster/features", params={"date": days[-1].isoformat()})
    data = response.json()
    assert data["format"] == "limit-pattern-features/1"
    assert data["samples"] and len(data["samples"][0]["vector"]) == len(data["feature_names"])
    assert all(s["window"][-1] < s["limit_date"] for s in data["samples"])
    csv_text = client.get("/api/cluster/features", params={"format": "csv"}).content.decode("utf-8-sig")
    from backend.app.services.cluster_service import dataset_from_csv

    back = dataset_from_csv(csv_text)
    assert len(back["samples"]) == len(data["samples"])


def test_cluster_run_and_cli(client, tmp_path):
    pytest.importorskip("sklearn")
    days = _seed_limitups()
    result = client.post("/api/cluster/run", json={"date": days[-1].isoformat(), "k": 2}).json()
    assert result["k"] == 2
    assert sum(c["size"] for c in result["clusters"]) == result["sample_count"]
    assert len(result["points"]) == result["sample_count"]
    assert len(result["clusters"][0]["pattern"]) == 3
    assert client.get("/api/cluster").json()["k"] == 2
    text = client.get("/api/export", params={"type": "cluster", "date": days[-1].isoformat()})
    assert text.status_code == 200 and "组1" in text.content.decode("utf-8-sig")
    agg = client.post("/api/cluster/run", json={"k": "auto", "method": "agglomerative"}).json()
    assert agg["method"] == "agglomerative"
    features = tmp_path / "f.json"
    features.write_text(client.get("/api/cluster/features").text, encoding="utf-8")
    from backend.app.tools.cluster import main

    out = tmp_path / "r.json"
    assert main(["--input", str(features), "--k", "auto", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["clusters"]
