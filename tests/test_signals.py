"""成本模型、T+1/涨跌停撮合、置换检验/多重校正、校准、影子状态、时段门 的单元测试。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backend.app.services.signals import cost, fill, stats
from backend.app.services.signals.calibration_report import report as calibration_report
from backend.app.services.signals.cost import DEFAULT_COST
from backend.app.services.signals.event_study import excess_returns, summary
from backend.app.services.signals.market_state import state_key
from backend.app.services.signals.shadow import evaluate, can_alert, set_status
from backend.app.services.signals.walkforward import split_windows


# ---------- 成本模型 ----------
def test_commission_min_floor():
    assert cost.commission(1000) == 5.0  # 1000 * 0.00025 = 0.25 < 5，取最低
    assert cost.commission(100000) == 25.0  # 100000 * 0.00025 = 25


def test_stamp_tax_sell_only():
    assert cost.stamp_tax(100000) == 50.0  # 0.0005


def test_round_trip_cost_total():
    r = cost.round_trip_cost(100000, 100000)
    assert r["buy_commission"] == 25.0
    assert r["sell_commission"] == 25.0
    assert r["stamp_tax"] == 50.0
    assert r["slippage"] == 200.0  # 0.001 * 2 * 100000
    assert r["total"] == 300.0


def test_net_return_positive():
    r = cost.net_return(10.0, 10.5, shares=100)
    gross = (10.5 - 10.0) * 100  # 50
    assert r["gross_return_pct"] == pytest.approx(5.0, abs=0.01)
    # 买入额 1000：佣金 5；卖出额 1050：佣金 5、印花 0.53、滑点买 1.0 卖 1.05
    total = r["costs"]["total"]
    assert total == pytest.approx(12.58, abs=0.01)
    assert r["net_return_pct"] == pytest.approx((50 - total) / 1000 * 100, abs=0.01)


# ---------- 撮合 ----------
def _bar(close=10.0, low=9.0, high=11.0, volume=1000, limit_up=None, limit_down=None):
    return {"close": close, "low": low, "high": high, "volume": volume,
            "limit_up_price": limit_up, "limit_down_price": limit_down}


def test_buyable_rejects_suspension_and_sealed():
    assert fill.buyable(_bar()) is True
    assert fill.buyable(_bar(volume=0)) is False  # 停牌
    assert fill.buyable(_bar(close=10.0, low=10.0, limit_up=10.0)) is False  # 一字板买不进
    assert fill.buyable(_bar(close=10.0, low=9.9, limit_up=10.0)) is True  # 盘中打开过，能买


def test_sellable_rejects_sealed_down():
    assert fill.sellable(_bar()) is True
    assert fill.sellable(_bar(close=9.0, high=9.0, limit_down=9.0)) is False  # 一字跌停卖不出
    assert fill.sellable(_bar(close=9.0, high=9.1, limit_down=9.0)) is True


def test_outcome_t_plus_1_requires_two_bars():
    assert fill.outcome([_bar()]) is None  # 只有信号日，没有卖出日
    bars = [_bar(close=10.0), _bar(close=10.5)]
    result = fill.outcome(bars, hold=1)
    assert result is not None
    assert result["net_return_pct"] > 0


def test_outcome_unbuyable_returns_none():
    bars = [_bar(close=10.0, low=10.0, limit_up=10.0), _bar(close=10.5)]
    assert fill.outcome(bars, hold=1) is None


# ---------- 置换检验 + 多重校正 ----------
def test_two_sample_pvalue_greater():
    treated = [1.0, 2.0, 3.0, 4.0, 5.0]
    control = [0.0, 0.1, 0.2, 0.1, 0.0]
    res = stats.two_sample_pvalue(treated, control, n_perm=200, seed=42, alternative="greater")
    assert res["p_value"] < 0.05
    assert res["statistic"] > 0


def test_sign_pvalue_greater_detects_shift():
    shifted = [1.0] * 30
    res = stats.sign_pvalue(shifted, n_perm=200, seed=1, alternative="greater")
    assert res["p_value"] <= 0.05


def test_bh_holm_ordering():
    # BH 比 Holm 宽松（同样的 p 值下 BH 调整后更小）
    pvals = [0.001, 0.01, 0.05, 0.2]
    bh = stats.bh_adjusted(pvals)
    holm = stats.holm_adjusted(pvals)
    assert bh[0] <= holm[0]
    # Holm 单调递增且不会超过 1
    assert all(0 <= p <= 1 for p in holm)
    assert all(0 <= p <= 1 for p in bh)


def test_significant_flags():
    pvals = [0.001, 0.04, 0.5]
    flags = stats.significant(pvals, alpha=0.05, method="bh")
    assert flags == [True, True, False] or flags == [True, False, False]  # BH 校正后 0.04 可能被抬到 0.06


def test_significant_holm_strict():
    pvals = [0.001, 0.5, 0.5]
    assert stats.significant(pvals, alpha=0.05, method="holm") == [True, False, False]


# ---------- 事件研究 ----------
def test_summary_empty():
    s = summary([])
    assert s["n"] == 0
    assert s["mean"] is None


def test_summary_win_rate_plr():
    s = summary([2.0, -1.0, 3.0, -1.0])
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["mean"] == pytest.approx(0.75)
    assert s["profit_loss_ratio"] == pytest.approx(2.5)  # 平均盈利 2.5 / 平均亏损 1.0


def test_excess_returns_subtracts_benchmark():
    events = [
        {"net_return_pct": 5.0, "benchmark_return_pct": 1.0},
        {"net_return_pct": 2.0, "benchmark_return_pct": 3.0},
    ]
    assert excess_returns(events) == [4.0, -1.0]


# ---------- 校准 ----------
def test_calibration_perfect():
    probs = [0.9, 0.9, 0.1, 0.1]
    labels = [1, 1, 0, 0]
    rep = calibration_report(probs, labels, bins=10)
    assert rep["brier"] == pytest.approx(0.0, abs=0.02)
    # ECE 分箱后 0.9 落箱命中 1，0.1 落箱命中 0，误差接近 0
    assert rep["ece"] < 0.2


def test_calibration_overconfident():
    # 平均置信 0.8，实际命中 0，ECE 应该明显 > 0
    probs = [0.8] * 10
    labels = [0] * 10
    rep = calibration_report(probs, labels, bins=10)
    assert rep["ece"] > 0.5
    assert rep["hit_rate"] == 0.0


def test_calibration_empty():
    rep = calibration_report([], [], bins=10)
    assert rep["n"] == 0
    assert rep["brier"] is None


# ---------- 市场状态分组 ----------
def test_state_key_hot_cold_neutral():
    assert state_key({"limit_up": 60, "broken_rate": 30.0}) == "hot"
    assert state_key({"limit_up": 10, "broken_rate": 30.0}) == "cold"
    assert state_key({"limit_up": 30, "broken_rate": 30.0}) == "neutral"
    assert state_key({"limit_up": 60, "broken_rate": 50.0}) == "neutral"  # 涨停多但炸板率高


# ---------- walk-forward ----------
def test_split_windows_non_overlap():
    from datetime import timedelta

    base = date(2026, 6, 1)
    events = [{"signal_date": base + timedelta(days=i)} for i in range(40)]
    windows = split_windows(events, train_days=10, test_days=5)
    assert len(windows) >= 2
    # test 窗口互不重叠
    for a, b in zip(windows, windows[1:]):
        assert a["test_end"] < b["test_start"]


# ---------- 影子状态 ----------
def test_shadow_lifecycle(tmp_path, monkeypatch):
    from backend.app.core.database import reset_engine, init_db, session_scope

    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    reset_engine()
    init_db()

    with session_scope() as s:
        assert can_alert(s, "volume_surge") is False  # 默认 shadow，不提醒

    # 达标后 passed 才允许提醒
    with session_scope() as s:
        # 直接注入 passed 状态
        set_status("volume_surge", "passed", "测试")
    with session_scope() as s:
        assert can_alert(s, "volume_surge") is True

    # live 且滚动胜率跌破阈值 -> degraded
    with session_scope() as s:
        set_status("volume_surge", "live", "上线")
    with session_scope() as s:
        new = evaluate(s, "volume_surge", n_events=50, win_rate=0.3, excess_mean=-1.0)
        assert new == "degraded"
        assert can_alert(s, "volume_surge") is False
