"""档4：形态聚类与向量检索。

这一份文件盯三件事：

* **两种后端是同一把尺子**。faiss 与 numpy 逐元素比对，容差按实测给（float32 转换那一档），
  并配一条「列序被打乱也能过」的负向控制——不配那条，逐元素比对就是个假判据。
* **聚类空间就是打分空间**。榜上的分数、回测、匹配明细与这里的组距离来自同一个
  RobustScaler + 同一份权重开方，不然「组」和「分」讲的是两个故事。
* **分组只吃库里落库的样本**。样本不足就说不足（400 带条数），旧口径的行如实报数，
  归属算不了就给原因，绝不回一张空表或一个编出来的组名。
"""

from __future__ import annotations

import json
import statistics
from datetime import date

import numpy as np
import pytest
from seeds import seed

from backend.app.algorithms.clustering import (
    MAX_SEPARATION,
    ClusterSignature,
    amplitude_word,
    base_name,
    disambiguate,
    momentum_word,
    name_clusters,
    position_word,
    signature_from_payloads,
    volume_word,
    weighted_space,
)
from backend.app.algorithms.features import (
    VECTOR_DIM,
    build_three_day_window,
    dimension_weights,
)
from backend.app.algorithms.similarity import (
    fit_robust_scaler,
    score_components,
    score_matrix,
)
from backend.app.algorithms.vector_index import (
    BACKEND_FAISS,
    BACKEND_NUMPY,
    VectorIndex,
    faiss_available,
    resolve_backend,
)
from backend.app.core.constants import (
    CLUSTER_MAX_K,
    CLUSTER_MIN_K,
    CLUSTER_MIN_SAMPLES,
    FAISS_PAIR_THRESHOLD,
)
from backend.app.core.database import session_scope
from backend.app.models.tables import LimitUpPattern
from backend.app.services import feature_service, pattern_service
from backend.app.services.history_backfill import load_bars

#: 20 个交易日、涨停集中在区间尾部，样本池（最后三个交易日）里能落进 7 条样本。
POOL_COUNT = 20
POOL_LIMITS = {15, 16, 17}


def _require_faiss() -> None:
    if not faiss_available():
        pytest.fail(
            "requirements.txt 里钉了 faiss-cpu==1.15.1，这个环境却没装上：一致性判据根本没跑，不能算通过。"
        )


def _seed_pool() -> list[date]:
    days = seed(POOL_COUNT, limit_indices=POOL_LIMITS)
    with session_scope() as session:
        stats = feature_service.new_stats("build_patterns", days[0], days[-1])
        for day in days:
            pattern_service.build_patterns(session, day, stats=stats)
    return days


def _stored_pool(as_of: date) -> tuple[list[date], list[tuple[str, str]]]:
    """库里样本池那几天的 (可用样本 key, 池子日期)。与 `cluster_service` 同一道过滤。"""
    with session_scope() as session:
        dates, rows = pattern_service.pool_patterns(session, as_of)
        keys = [
            (row.stock_code, row.limit_up_date.isoformat())
            for row in rows
            if len(pattern_service.vector_of(row)) == VECTOR_DIM
        ]
    return dates, keys


# --- 向量检索：两种后端必须给出同一批距离 -------------------------------------


def _blobs(rows: int, cols: int, seed_value: int) -> np.ndarray:
    return np.random.default_rng(seed_value).normal(0.0, 2.0, size=(rows, cols))


def test_faiss_and_numpy_distances_agree_elementwise() -> None:
    _require_faiss()
    samples = _blobs(400, VECTOR_DIM, 20261025)
    queries = _blobs(300, VECTOR_DIM, 777)
    # 掺几条「与样本几乎相同」的查询：距离贴到 0 附近，那正是浮点最容易出差的地方。
    queries[:20] = samples[:20] + _blobs(20, VECTOR_DIM, 4242) * 1e-6

    numpy_matrix = VectorIndex(samples, BACKEND_NUMPY).distances(queries)
    faiss_matrix = VectorIndex(samples, BACKEND_FAISS).distances(queries)

    diff = np.abs(numpy_matrix - faiss_matrix)
    # 实测这一批数据上的最大差在 4e-6 量级（距离），换成欧氏分是 5e-5 分——
    # 落在两位小数之下，也落在分档阈值 90/80/70/60 的任何一档边界之外。
    assert float(diff.max()) < 1e-4, f"两种后端的距离对不上，最大差 {float(diff.max())}"
    score_diff = np.abs(100.0 * np.exp(-numpy_matrix) - 100.0 * np.exp(-faiss_matrix))
    assert float(score_diff.max()) < 1e-3
    # 自匹配那 20 行：两边都必须是零距离附近，不是「差得多但都算近」。
    diagonal = numpy_matrix[np.arange(20), np.arange(20)]
    assert float(diagonal.max()) < 1e-4
    assert np.allclose(faiss_matrix[np.arange(20), np.arange(20)], diagonal, atol=1e-4)


def test_elementwise_comparison_would_catch_a_permuted_result() -> None:
    """负向控制：faiss 回的是按距离升序排好的结果，列序必须用 `indices` 摆回去。

    少了那一步摆回，每一行仍然是「一组正确的距离」，只是配错了股票。
    这条判据证明上面那个逐元素比对不是恰好通过——列序一乱它立刻变红。
    """
    _require_faiss()
    samples = _blobs(200, VECTOR_DIM, 20261025)
    queries = _blobs(80, VECTOR_DIM, 777)
    numpy_matrix = VectorIndex(samples, BACKEND_NUMPY).distances(queries)

    import faiss

    index = faiss.IndexFlatL2(VECTOR_DIM)
    index.add(np.ascontiguousarray(samples, dtype=np.float32))
    sorted_only, _indices = index.search(
        np.ascontiguousarray(queries, dtype=np.float32), samples.shape[0]
    )
    permuted = np.sqrt(np.maximum(sorted_only, 0.0)).astype(np.float64)
    assert float(np.abs(numpy_matrix - permuted).max()) > 1.0


def test_auto_backend_switches_on_pair_count_not_on_opinion() -> None:
    _require_faiss()
    assert resolve_backend("auto", n_queries=100, n_samples=100) == BACKEND_NUMPY
    assert resolve_backend("auto", n_queries=300, n_samples=200) == BACKEND_FAISS
    assert 100 * 100 < FAISS_PAIR_THRESHOLD <= 300 * 200
    small = _blobs(100, 8, 1)
    big = _blobs(200, 8, 2)
    assert VectorIndex.build(small, n_queries=100).backend == BACKEND_NUMPY
    assert VectorIndex.build(big, n_queries=300).backend == BACKEND_FAISS
    # 显式要一个不存在的路，不静默降级。
    assert VectorIndex.build(small, backend=BACKEND_FAISS, n_queries=1).backend == BACKEND_FAISS


# --- 聚类空间与打分空间是同一个空间 ------------------------------------------


def test_weighted_space_is_the_space_the_scores_come_from() -> None:
    samples = _blobs(12, VECTOR_DIM, 5150)
    targets = _blobs(4, VECTOR_DIM, 6060)
    median, iqr = fit_robust_scaler(samples)
    weights = np.asarray(dimension_weights(), dtype=np.float64)
    weights = weights / float(weights.sum())
    sw = (samples - median) / iqr * np.sqrt(weights)
    tw = (targets - median) / iqr * np.sqrt(weights)

    assert np.allclose(weighted_space(samples), sw)
    manual = np.linalg.norm(tw[:, None, :] - sw[None, :, :], axis=2)
    blended, euclidean, _cosine = score_matrix(targets, samples)
    assert np.allclose(euclidean, 100.0 * np.exp(-manual), atol=1e-9)
    # 同一条向量对自身的欧氏分是满分，加权空间里距离也是 0：两把尺子在这一点上必须重合。
    assert float(euclidean[0, 0]) == pytest.approx(100.0 * np.exp(-float(manual[0, 0])))
    assert float(np.linalg.norm(sw[0] - sw[0])) == 0.0
    assert blended.shape == manual.shape


def test_group_names_come_from_the_word_tables_and_boundaries() -> None:
    # 每一档的边界值都能核对，改阈值就要改这里。
    assert momentum_word(12.0) == "加速拉升"
    assert momentum_word(11.99) == "强势上行"
    assert momentum_word(-6.0) == "小幅回落"
    assert momentum_word(-6.01) == "深度回调"
    assert volume_word(1.80) == "急剧放量"
    assert volume_word(1.00) == "量能平稳"
    assert volume_word(0.84) == "缩量整理"
    assert position_word(0.85) == "高位区"
    assert position_word(0.20) == "低位区"
    assert amplitude_word(12.0) == "大幅震荡"
    assert amplitude_word(5.9) == "窄幅波动"

    signature = ClusterSignature(
        return_3d=8.0,
        slope_close=4.0,
        volatility_3d=2.0,
        volume_trend=1.3,
        amplitude_sum=9.0,
        amplitude_mean=3.0,
        turnover_rate=2.5,
        position_20=0.7,
    )
    assert base_name(signature) == "强势上行·持续放量·中高位"
    assert "T-1 换手 2.50%" in signature.describe()

    # 同名先补振幅档；连振幅档都一样才加序号。
    low_amplitude = ClusterSignature(**{**signature.__dict__, "amplitude_sum": 3.0})
    once, twice = disambiguate([base_name(signature), base_name(signature)], [signature, low_amplitude])
    assert once == "强势上行·持续放量·中高位·振幅适中"
    assert twice == "强势上行·持续放量·中高位·窄幅波动"
    stamped = name_clusters([signature, signature, signature])
    assert stamped == [f"强势上行·持续放量·中高位·振幅适中 #{i}" for i in (1, 2, 3)]

    missing = ClusterSignature(**{**signature.__dict__, "turnover_rate": None})
    assert "T-1 换手 —" in missing.describe()
    assert missing.as_dict()["turnover_rate"] is None


def test_signature_is_a_median_of_stored_features_not_a_centroid() -> None:
    payloads = [
        {
            "aggregates": {
                "return_3d": 2.0,
                "slope_close": 1.0,
                "volatility_3d": 0.5,
                "volume_trend": 1.2,
                "amplitude_sum": 9.0,
            },
            "days": [
                {"features": {"amplitude": 3.0, "turnover_rate": 1.0, "position_20": 0.4}},
                {"features": {"amplitude": 5.0, "turnover_rate": 2.0, "position_20": 0.6}},
                {"features": {"amplitude": 7.0, "turnover_rate": 3.0, "position_20": 0.8}},
            ],
        },
        {
            "aggregates": {
                "return_3d": 8.0,
                "slope_close": 3.0,
                "volatility_3d": 1.5,
                "volume_trend": 2.0,
                "amplitude_sum": 15.0,
            },
            "days": [
                {"features": {"amplitude": 5.0, "turnover_rate": 5.0, "position_20": 0.5}},
                {"features": {"amplitude": 6.0, "turnover_rate": 6.0, "position_20": 0.6}},
                {"features": {"amplitude": 7.0, "turnover_rate": 7.0, "position_20": 0.9}},
            ],
        },
    ]
    signature = signature_from_payloads(payloads)
    assert signature.return_3d == statistics.median([2.0, 8.0])
    assert signature.volume_trend == statistics.median([1.2, 2.0])
    assert signature.amplitude_sum == statistics.median([9.0, 15.0])
    # 日均振幅先按样本各自取三天均值，再在组内取中位数。
    assert signature.amplitude_mean == statistics.median([5.0, 6.0])
    # 换手率与位置读的是形态三天的最后一天（T-1），不是涨停日。
    assert signature.turnover_rate == statistics.median([3.0, 7.0])
    assert signature.position_20 == statistics.median([0.8, 0.9])

    empty = signature_from_payloads([{"aggregates": None, "days": []}])
    assert empty.return_3d == 0.0
    assert empty.turnover_rate is None  # 缺列就是缺列，不写成 0.00%


# --- 分组接口：吃库里的样本池 -----------------------------------------------


def test_cluster_overview_groups_the_stored_pool(client) -> None:
    days = _seed_pool()
    as_of = days[-1]
    pool_dates, keys = _stored_pool(as_of)
    assert len(keys) >= CLUSTER_MIN_SAMPLES, "夹具没有攒出够聚类的样本池，后面的判据都是空的"

    response = client.get("/api/clusters/overview", params={"date": as_of.isoformat()})
    assert response.status_code == 200, response.text
    payload = response.json()
    # 页面读数吃库里那一版：样本数与 `limit_up_patterns` 里可用行数一字不差。
    assert payload["sample_count"] == len(keys)
    assert payload["pool_dates"] == [day.isoformat() for day in pool_dates]
    assert payload["ignored_stale"] == 0
    assert CLUSTER_MIN_K <= payload["k"] <= CLUSTER_MAX_K
    assert payload["selected_by"] in {"轮廓系数最大", "样本不足，不分组"}
    assert -1.0 <= payload["silhouette"] <= 1.0

    sizes = [item["size"] for item in payload["clusters"]]
    assert sum(sizes) == payload["sample_count"], "有条本没被分到任何一组"
    members = [(item["code"], item["limit_date"]) for group in payload["clusters"] for item in group["members"]]
    assert len(members) == len(set(members)), "同一条样本落进了两个组"
    assert set(members) == set(keys), "分组成员与库里的样本池不是同一批"
    names = [item["name"] for item in payload["clusters"]]
    assert len(names) == len(set(names)), "组名没有消歧，页面上会出现两个同名组"
    for group in payload["clusters"]:
        assert group["representative"] in group["members"]
        assert 0.0 < group["share"] <= 1.0
        assert group["intra_distance"] >= 0.0
        assert 0.0 <= group["separation"] <= MAX_SEPARATION
        assert group["signature"]["return_3d"] is not None
        assert any(word in group["name"] for word in ("上行", "整理", "回落", "拉升", "回调"))
        # 画像那一列不是空壳：它就是这组中位数那句话，换手缺列时写「—」而不是 0.00%。
        assert group["description"].startswith("三日累计")
        assert f"量能 {group['signature']['volume_trend']:.2f}×" in group["description"]
        turnover = group["signature"]["turnover_rate"]
        assert (f"T-1 换手 {'—' if turnover is None else f'{turnover:.2f}%'}") in group["description"]

    # 散点：每条样本一个点，每组一个质心；只服务图，不参与任何分数。
    assert len(payload["points"]) == payload["sample_count"] + len(payload["clusters"])
    assert sum(1 for item in payload["points"] if item["is_centroid"]) == len(payload["clusters"])

    # NaN / Infinity 一旦进 JSON，前端 `response.json()` 当场失败、整页空白。
    assert "NaN" not in response.text and "Infinity" not in response.text
    json.loads(response.text)


def test_clustering_is_deterministic(client) -> None:
    days = _seed_pool()
    as_of = days[-1].isoformat()
    first = client.get("/api/clusters/overview", params={"date": as_of}).json()
    second = client.get("/api/clusters/overview", params={"date": as_of}).json()
    assert first["k"] == second["k"]
    assert [item["name"] for item in first["clusters"]] == [item["name"] for item in second["clusters"]]
    assert [
        [(member["code"], member["limit_date"]) for member in item["members"]]
        for item in first["clusters"]
    ] == [
        [(member["code"], member["limit_date"]) for member in item["members"]]
        for item in second["clusters"]
    ], "同一批样本两次打开给出不同分组，使用者没法追踪"


def test_include_members_false_keeps_the_groups(client) -> None:
    days = _seed_pool()
    payload = client.get(
        "/api/clusters/overview", params={"date": days[-1].isoformat(), "include_members": "false"}
    ).json()
    assert all(item["members"] == [] for item in payload["clusters"])
    assert all(item["representative"]["code"] for item in payload["clusters"])


def test_explicit_k_is_honoured_and_bounded_by_the_pool(client) -> None:
    days = _seed_pool()
    as_of = days[-1].isoformat()
    payload = client.get("/api/clusters/overview", params={"date": as_of, "k": 2}).json()
    assert payload["k"] == 2
    assert payload["selected_by"] == "调用方指定"
    assert payload["evaluated"] == []
    assert len(payload["clusters"]) == 2

    pool_count = payload["sample_count"]
    over = client.get("/api/clusters/overview", params={"date": as_of, "k": pool_count + 3})
    assert over.status_code == 400
    assert "不能大于等于样本数" in over.json()["detail"]


def test_thin_pool_says_how_many_are_missing(client) -> None:
    """负向控制：样本不足时回 400 并报条数，不回一张看起来像「今天只有一类形态」的空表。"""
    days = seed(POOL_COUNT, limit_indices={19})
    with session_scope() as session:
        stats = feature_service.new_stats("build_patterns", days[0], days[-1])
        pattern_service.build_patterns(session, days[-1], stats=stats)
    _dates, keys = _stored_pool(days[-1])
    assert len(keys) < CLUSTER_MIN_SAMPLES

    response = client.get("/api/clusters/overview", params={"date": days[-1].isoformat()})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert f"只有 {len(keys)} 条" in detail
    assert "重建涨停样本" in detail

    assigned = client.get("/api/clusters/600000", params={"date": days[-1].isoformat()})
    assert assigned.status_code == 400


def test_empty_database_and_unknown_day_are_told_apart(client) -> None:
    missing = client.get("/api/clusters/overview", params={"date": "2026-06-12"})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "这一天没有行情"


def test_stale_vectors_are_skipped_and_counted(client) -> None:
    """旧口径写的行（向量长度不对）不进聚类，但要在 `ignored_stale` 里如实报数。"""
    days = _seed_pool()
    as_of = days[-1]
    _dates, keys = _stored_pool(as_of)
    with session_scope() as session:
        session.add(
            LimitUpPattern(
                stock_code="600999",
                limit_up_date=as_of,
                window_dates=[],
                feature_vector=[0.0, 0.0, 0.0],
                day_details=[],
            )
        )
    payload = client.get("/api/clusters/overview", params={"date": as_of.isoformat()}).json()
    assert payload["sample_count"] == len(keys)
    assert payload["ignored_stale"] == 1
    assert ("600999", as_of.isoformat()) not in [
        (item["code"], item["limit_date"]) for group in payload["clusters"] for item in group["members"]
    ]


# --- 归属：与榜、回测、匹配明细同一把尺子 ------------------------------------


def test_assignment_excludes_the_target_from_its_own_group(client) -> None:
    days = _seed_pool()
    as_of = days[-1]
    _dates, keys = _stored_pool(as_of)
    in_pool = [code for code, _day in keys]
    assert "600000" in in_pool, "夹具里 600000 应当在样本池里，自匹配判据才有意义"

    response = client.get("/api/clusters/600000", params={"date": as_of.isoformat()})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == "600000"
    assert payload["window"] and len(payload["window"]) == 3
    assert payload["assigned_cluster_id"] is not None
    assert payload["groups"]
    for group in payload["groups"]:
        assert all(item["code"] != "600000" for item in group["top_members"]), "自己出现在了自己的对照组里"
        assert group["max_similarity"] < 100.0, "自匹配那条 100 分没被剔掉"
    assert 0.0 < payload["margin"] <= 100.0
    assert "NaN" not in response.text and "Infinity" not in response.text


def test_assignment_scores_come_from_score_components(client) -> None:
    """页面上那一列「组内均分」必须能从 `score_components` 复算出来——同一把尺子。

    比对用**未取整**的分数：服务那边是先平均再保留两位，若这里先取整再平均，
    最后一分钱能对不上，而那不是 bug。

    组成员从 `overview` 那一路取：归属接口只回 `top_members`（最多五条），
    拿它当全组样本去复算「组内均分」，组一大就必然对不上。
    """
    days = _seed_pool()
    as_of = days[-1]
    target = "600001"
    payload = client.get(f"/api/clusters/{target}", params={"date": as_of.isoformat()}).json()
    assert payload["skipped_reason"] is None

    with session_scope() as session:
        _dates, rows = pattern_service.pool_patterns(session, as_of)
        rows = [row for row in rows if len(pattern_service.vector_of(row)) == VECTOR_DIM]
        vectors = [pattern_service.vector_of(row) for row in rows]
        keys = [(row.stock_code, row.limit_up_date.isoformat()) for row in rows]
        window = build_three_day_window(
            load_bars(session, target, as_of), as_of, include_as_of_date=True
        )
        assert window.ok

    raw = score_components(
        np.asarray(window.vector, dtype=np.float64),
        np.asarray(vectors, dtype=np.float64),
    )["score"][0]
    keyed = {key: float(value) for key, value in zip(keys, raw)}
    grouped = {
        group["cluster_id"]: [
            (item["code"], item["limit_date"])
            for item in group["members"]
            if item["code"] != target
        ]
        for group in client.get(
            "/api/clusters/overview", params={"date": as_of.isoformat()}
        ).json()["clusters"]
    }

    for group in payload["groups"]:
        usable = grouped[group["cluster_id"]]
        assert usable, "这一组除了目标自己没别人，不该出现在归属表里"
        values = [keyed[key] for key in usable]
        assert group["size"] == len(values)
        assert group["avg_similarity"] == round(statistics.mean(values), 2)
        assert group["max_similarity"] == round(max(values), 2)
        assert group["high_band_count"] == sum(1 for value in values if value >= payload["high_band"])
        for member in group["top_members"]:
            assert member["similarity"] == round(
                keyed[(member["code"], member["limit_date"])], 2
            ), f"{member['code']} 的分数与现算对不上"

    ranked = sorted(payload["groups"], key=lambda item: -item["avg_similarity"])
    assert payload["assigned_cluster_id"] == ranked[0]["cluster_id"]
    if len(ranked) > 1:
        assert payload["margin"] == round(ranked[0]["avg_similarity"] - ranked[1]["avg_similarity"], 2)
    else:
        # 只剩一个可比组时没有「次优组」可减，服务直接报这一组的均分，不编一个差距出来。
        assert payload["margin"] == round(ranked[0]["avg_similarity"], 2)


def test_assignment_without_a_window_gives_the_reason(client) -> None:
    """库里没这段行情的股票：回 200 但说清算不了的原因，不给一个编出来的归属。"""
    days = _seed_pool()
    response = client.get("/api/clusters/600999", params={"date": days[-1].isoformat()})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["assigned_cluster_id"] is None
    assert payload["assigned_cluster_name"] == ""
    assert payload["groups"] == []
    assert payload["window"] == []
    assert "凑不齐三天行情" in payload["skipped_reason"]


def test_assignment_rejects_a_code_outside_the_scope(client) -> None:
    days = _seed_pool()
    response = client.get("/api/clusters/688981", params={"date": days[-1].isoformat()})
    assert response.status_code == 422
