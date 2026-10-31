"""把 docs/formulas 两份文档里的实测数字逐条钉住（AI打分公式 §7、前100挑选 §5/§7）。"""

import math

import numpy as np
import pytest

from backend.app.algorithms.features import GROUPS, dimension_weights, feature_groups, feature_names
from backend.app.algorithms.similarity import (
    SimilarityEngine,
    decay_score,
    describe_band,
    fit_robust_scaler,
    median_pairwise_distance,
    score_matrix,
    summarize,
)
from backend.app.services.backtest_service import band_of
from backend.app.services.score_model import (
    _volume_points,
    hit_rate,
    level_of,
    prediction_score,
    read_composite,
    risk_penalty,
    risk_score_of,
    score_from_history,
    score_of,
    technical_score_of,
    volume_score_of,
    weighted_carry_rate,
)


def test_composite_with_capital_present_47_2():
    got = score_of({"similarity_score": 92, "technical_score": 70, "volume_score": 60, "capital_score": 50,
                    "history_limit_up_rate": 30, "risk_score": 85})
    assert got["composite_score"] == 47.2
    assert got["level"] == "低匹配"


def test_composite_capital_missing_65_24():
    got = score_of({"similarity_score": 92, "technical_score": 70, "volume_score": 60, "capital_score": None,
                    "history_limit_up_rate": 30, "risk_score": 55})
    assert got["composite_score"] == 65.24
    assert got["level"] == "一般"
    assert got["weight_total"] == 0.85
    assert abs(sum(v for v in got["contributions"].values() if v is not None) - got["weighted_score"]) < 0.02
    assert "capital_score" in got["missing_items"]


def test_no_signal_reads_as_dash():
    got = score_of({"pattern_match_rate": 50, "risk_score": 70})
    assert got["composite_score"] == 0.0 and got["has_signal"] is False
    assert read_composite(got) is None


def test_technical_62():
    assert technical_score_of({"position_10": 0.8, "position_20": 0.6, "position_60": 0.4, "body_percent": 50, "close_change": 3}) == 62.0


def test_volume_75_and_table():
    assert volume_score_of({"volume_ratio_5": 2.0, "volume_ratio_10": 1.0}) == 75.0
    table = {0: 30, 0.7: 51, 1: 60, 1.5: 72.5, 2: 85, 3: 95, 4: 95, -1: 0}
    for x, want in table.items():
        assert _volume_points(x) == pytest.approx(want)


def test_risk_63_5_and_down_day_29_17():
    assert risk_score_of({"position_20": 0.9, "amplitude": 12, "volume_ratio_5": 4}, change_percent=6) == 63.5
    assert risk_score_of({"position_20": 0.5}, change_percent=-3) == 29.17
    assert risk_score_of({"position_20": 0.5}, change_percent=None) == 50.0


def test_risk_and_level_edges():
    assert [risk_penalty(x) for x in (80, 79.99, 60, 59.99, 40, 39.99, None)] == [20, 10, 10, 5, 5, 0, 0]
    assert [level_of(x) for x in (90, 89.99, 60, 59.99, None)] == ["强关注", "高关注", "一般", "低匹配", ""]


def test_history_total_and_hit_rate():
    assert score_from_history(80, 3, 5) == 56
    assert score_from_history(77.6, 0, 0) == 78
    assert score_from_history(None, 0, 0) is None
    assert hit_rate(3, 7) == 30.0
    assert hit_rate(0, 0) is None


def test_prediction_scores():
    assert prediction_score(92, 40, 1.5)["prediction_score"] == 73.0
    short = prediction_score(92, 40, None)
    assert short["prediction_score"] == 72.5 and short["terms"]["band"] is None
    capped = prediction_score(92, None, 3.0)
    assert capped["prediction_score"] == 94.29 and capped["terms"]["band"] == 100
    assert weighted_carry_rate([(90, True), (40, True), (10, False)]) == 92.86
    assert weighted_carry_rate([]) is None


def test_band_labels():
    got = [band_of(x) for x in (95, 90, 89.9, 80, 79.9, 70, 69.9, 60, 59.9)]
    assert got == ["90~100", "90~100", "80~90", "80~90", "70~80", "70~80", "60~70", "60~70", "<60"]
    assert [describe_band(x) for x in (90, 80, 70, 60, 59.9)] == ["高度", "较高", "一般", "低", "弱"]


# ---------------- 前 100 挑选文档 ----------------


def test_vector_groups_and_weights():
    names = feature_names()
    groups = feature_groups()
    assert len(names) == 67
    weights = np.asarray(dimension_weights())
    sums = {g: round(float(weights[[i for i, x in enumerate(groups) if x == g]].sum()), 6) for g in GROUPS}
    assert sums == {"price_trend": 0.35, "kline_structure": 0.2, "volume": 0.15, "amount": 0.1, "turnover": 0.1,
                    "position": 0.05, "three_day_trend": 0.05}
    for day in ("t1", "t2", "t3"):
        assert round(float(weights[names.index(f"{day}_close_change")]), 5) == 0.04667


def test_decay_table():
    assert decay_score(0) == 100
    assert round(decay_score(0.1), 1) == 99.0
    assert round(decay_score(0.3), 1) == 91.4
    assert round(decay_score(0.5), 2) == 77.88
    assert round(decay_score(1.0), 2) == 36.79


def test_score_90_needs_d_within_0_3654_scale():
    assert round(math.sqrt(-math.log(0.875)), 4) == 0.3654


def test_robust_scaler_median_iqr_and_degenerate():
    data = np.asarray([[1.0, 5.0], [2.0, 5.0], [3.0, 5.0], [4.0, 5.0], [np.nan, 5.0]])
    center, scale = fit_robust_scaler(data)
    assert center[0] == 2.5 and scale[0] == pytest.approx(1.5)
    assert scale[1] == 1.0  # IQR 0 → 1


def test_missing_feature_is_median_after_scaling():
    rng = np.random.default_rng(1)
    pool = rng.normal(12, 2, size=(40, 67))
    engine = SimilarityEngine(0.8, 0.2).fit(pool)
    target = pool[0].copy()
    target[5] = np.nan
    z = engine.standardize(target.reshape(1, -1))
    assert z[0, 5] == 0.0  # 偏离 = 0，就是中位数（修了文档 §8 第 5 条）


def test_self_similarity_100_and_scale_is_median_pairwise():
    rng = np.random.default_rng(2)
    pool = rng.normal(size=(30, 67))
    engine = SimilarityEngine(0.8, 0.2).fit(pool)
    scores = engine.score(pool[:3])
    assert np.allclose([scores[i, i] for i in range(3)], 100)
    z = engine.standardize(pool)
    assert engine.distance_scale == pytest.approx(median_pairwise_distance(z))
    total, eu, co = score_matrix(pool[:1], pool)
    d = np.linalg.norm(z[0] - z[1])
    assert eu[0, 1] == pytest.approx(100 * math.exp(-((d / engine.distance_scale) ** 2)), abs=0.01)
    cos = float(z[0] @ z[1] / (np.linalg.norm(z[0]) * np.linalg.norm(z[1])))
    assert co[0, 1] == pytest.approx((cos + 1) / 2 * 100, abs=0.01)
    assert total[0, 1] == pytest.approx(round(0.8 * eu[0, 1] + 0.2 * co[0, 1], 2), abs=0.011)


def test_summary_four_numbers():
    got = summarize(np.asarray([95.0, 91.0, 80.0, 50.0]), 90)
    assert got["max_similarity"] == 95.0 and got["avg_similarity"] == 79.0
    assert got["high_similarity_count"] == 2 and got["sample_count"] == 4 and got["band"] == "高度"


def test_rank_key_tiebreak_by_code():
    rows = [("600001", 97), ("000002", 95), ("600003", 95), ("300009", 90)]
    top = sorted(rows, key=lambda r: (-r[1], -r[1], r[0]))[:3]
    assert [c for c, _ in top] == ["600001", "000002", "600003"]


def test_feed_order_limit_up_first_not_globally_sorted():
    from backend.app.services.similarity_service import feed_order

    items = [{"code": "a", "max_similarity": 99, "is_limit_up": False}, {"code": "b", "max_similarity": 70, "is_limit_up": True},
             {"code": "c", "max_similarity": 69, "is_limit_up": True}]
    assert [i["code"] for i in feed_order(items)] == ["b", "c", "a"]
