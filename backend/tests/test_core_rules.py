"""核心规则的直接测试：涨停、窗口、相似度、评分。"""

from datetime import date, timedelta

import numpy as np

from backend.app.algorithms.features import (
    VECTOR_DIM,
    Bar,
    build_three_day_window,
    dimension_weights,
)
from backend.app.algorithms.similarity import band_of, score_matrix, score_pair
from backend.app.services.limit_up_service import LimitUpDetector, limit_up_price, ratio_for
from backend.app.services.score_model import (
    FORMULA_TEXT,
    ITEMS,
    PROMPT_RUBRIC,
    PUBLIC_TEXTS,
    WEIGHT_CAPITAL,
    WEIGHT_HISTORY,
    WEIGHT_PATTERN,
    WEIGHT_TECHNICAL,
    WEIGHT_VOLUME,
    capital_score_of,
    pattern_match_rate,
    risk_penalty,
    score_of,
)


def _bars(n=30, start=date(2026, 1, 5), close=10.0, step=0.1):
    rows = []
    price = close
    day = start
    while len(rows) < n:
        if day.weekday() < 5:
            rows.append(
                Bar(
                    trade_date=day,
                    open=round(price - 0.05, 2),
                    high=round(price + 0.2, 2),
                    low=round(price - 0.2, 2),
                    close=round(price, 2),
                    pre_close=round(price - step, 2),
                    volume=1000 + len(rows) * 10,
                    amount=None,
                    turnover_rate=None,
                )
            )
            price += step
        day += timedelta(days=1)
    return rows


def test_vector_is_67_and_weights_sum_to_one():
    bars = _bars()
    window = build_three_day_window(bars, bars[-1].trade_date, include_as_of_date=True)
    assert window.ok
    assert len(window.vector) == VECTOR_DIM == 67
    assert abs(sum(dimension_weights()) - 1) < 1e-9


def test_sample_window_excludes_the_limit_up_day():
    bars = _bars(40)
    as_of = bars[-1].trade_date
    sample = build_three_day_window(bars, as_of, include_as_of_date=False)
    candidate = build_three_day_window(bars, as_of, include_as_of_date=True)
    assert sample.ok and candidate.ok
    assert sample.days[-1].trade_date < as_of
    assert candidate.days[-1].trade_date == as_of
    assert [bar.trade_date for bar in candidate.days][1:] == [bar.trade_date for bar in sample.days][:0] or (
        candidate.days[0].trade_date == sample.days[1].trade_date
    )
    assert candidate.days[-1].trade_date != sample.days[-1].trade_date


def test_future_bars_do_not_change_the_vector():
    bars = _bars(40)
    as_of = bars[-1].trade_date
    future = Bar(
        trade_date=as_of + timedelta(days=1),
        open=999,
        high=999,
        low=1,
        close=999,
        pre_close=1,
        volume=1,
    )
    left = build_three_day_window(bars, as_of, include_as_of_date=True)
    right = build_three_day_window(bars + [future], as_of, include_as_of_date=True)
    assert left.vector == right.vector


def test_short_history_is_rejected():
    bars = _bars(2)
    window = build_three_day_window(bars, bars[-1].trade_date, include_as_of_date=True)
    assert not window.ok
    assert window.reason == "insufficient_trading_days"


def test_identical_vectors_score_100_and_order_is_stable():
    bars = _bars(40)
    window = build_three_day_window(bars, bars[-1].trade_date, include_as_of_date=True)
    vector = np.nan_to_num(np.asarray(window.vector, dtype=float))
    pool = np.vstack([vector, vector * 1.5 + 1, vector * 0.5 - 1, vector + 2])
    scores, _eu, _co = score_matrix(vector, pool)
    assert scores[0, 0] == 100
    assert scores[0, 1] < scores[0, 0]
    # 单样本池：标准化后全 0，范数 ≤ 1e-9 时 cos 记 0，所以自比是 0.8·100 + 0.2·50 = 90（文档原样）
    assert score_pair(vector, vector) == 90


def test_self_match_can_be_masked_by_code():
    vector = np.asarray(_bars_vector(), dtype=float)
    samples = np.vstack([vector, vector + 3, vector])
    scores, _, _ = score_matrix(vector, samples)
    codes = ["000001", "000002", "000001"]
    kept = [float(scores[0, index]) for index, code in enumerate(codes) if code != "000001"]
    assert len(kept) == 1
    assert max(kept) < 100


def _bars_vector():
    bars = _bars(36)
    return build_three_day_window(bars, bars[-1].trade_date, include_as_of_date=False).vector


def test_main_board_st_ratio_changes_on_2026_07_06():
    assert ratio_for("main_board", True, date(2026, 7, 5)) == __import__("decimal").Decimal("0.05")
    assert ratio_for("main_board", True, date(2026, 7, 6)) == __import__("decimal").Decimal("0.10")
    assert ratio_for("chinext", True, date(2026, 1, 1)) == __import__("decimal").Decimal("0.20")
    assert ratio_for("star", False, date(2026, 8, 1)) == __import__("decimal").Decimal("0.20")
    assert ratio_for("bse", False, date(2026, 8, 1)) == __import__("decimal").Decimal("0.30")


def test_limit_price_rounds_half_up_and_gap_uses_price():
    price = limit_up_price(3.19, ratio_for("main_board", False, date(2026, 9, 28)))
    assert float(price) == 3.51
    gap = LimitUpDetector().distance_to_limit(pre_close=3.19, latest=3.19, limit_price=float(price))
    assert gap == 10.03
    sealed = LimitUpDetector().distance_to_limit(pre_close=3.19, latest=3.51, limit_price=3.51)
    assert sealed == 0.0
    assert LimitUpDetector().distance_to_limit(pre_close=None, latest=3.19, limit_price=3.51) is None
    dirty = LimitUpDetector().distance_to_limit(pre_close=3.19, latest=4, limit_price=3.51)
    assert dirty == 0.0


def test_close_at_generic_limit_drops_the_st_cap():
    detector = LimitUpDetector()
    # 前收 10，5% 涨停价 10.50，10% 涨停价 11.00。收在 11 说明那天没戴帽。
    label = detector.label_bar(
        board="main_board",
        is_st=True,
        trade_date=date(2026, 6, 1),
        open_=10,
        high=11,
        low=10,
        close=11,
        pre_close=10,
        volume=100,
        list_date=date(2020, 1, 2),
        trading_days=_calendar(date(2020, 1, 2), date(2026, 6, 2)),
    )
    assert label.is_limit_up
    assert label.limit_up_percent == 10
    assert label.limit_up_price == 11.0


def test_ex_rights_beyond_generic_band_stays_tight():
    detector = LimitUpDetector()
    label = detector.label_bar(
        board="main_board",
        is_st=True,
        trade_date=date(2026, 6, 1),
        open_=8,
        high=8,
        low=7,
        close=7.5,
        pre_close=10,
        volume=100,
        list_date=date(2020, 1, 2),
        trading_days=_calendar(date(2020, 1, 2), date(2026, 6, 2)),
    )
    assert not label.is_limit_up
    assert label.limit_up_percent == 5


def test_ipo_first_five_days_are_not_limit_up():
    listing = date(2026, 6, 1)
    calendar = _calendar(listing, date(2026, 6, 20))
    fifth = [day for day in calendar if day >= listing][4]
    sixth = [day for day in calendar if day >= listing][5]
    detector = LimitUpDetector()
    early = detector.label_bar(
        board="chinext",
        is_st=False,
        trade_date=fifth,
        open_=10,
        high=20,
        low=10,
        close=20,
        pre_close=10,
        volume=100,
        list_date=listing,
        trading_days=calendar,
    )
    later = detector.label_bar(
        board="chinext",
        is_st=False,
        trade_date=sixth,
        open_=10,
        high=12,
        low=10,
        close=12,
        pre_close=10,
        volume=100,
        list_date=listing,
        trading_days=calendar,
    )
    assert early.reason == "ipo_no_limit"
    assert not early.is_limit_up
    assert later.is_limit_up
    assert later.limit_up_percent == 20


def test_missing_amount_does_not_make_the_bar_invalid():
    label = LimitUpDetector().label_bar(
        board="main_board",
        is_st=False,
        trade_date=date(2026, 8, 3),
        open_=10,
        high=11,
        low=10,
        close=11,
        pre_close=10,
        volume=100,
        list_date=None,
        trading_days=[],
    )
    assert label.is_limit_up


def test_band_edges():
    assert band_of(90) == "90~100"
    assert band_of(89.99) == "80~90"
    assert band_of(60) == "60~70"
    assert band_of(59.99) == "<60"


def test_score_model_full_and_missing_capital():
    assert abs(sum(weight for _k, _f, weight in ITEMS) - 1) < 1e-9
    full = score_of(
        {
            "similarity_score": 80,
            "technical_score": 60,
            "volume_score": 60,
            "capital_score": 60,
            "history_limit_up_rate": 60,
            "risk_score": 10,
        }
    )
    assert full["composite_score"] == 67
    assert full["level"] == "一般"
    assert full["contributions"]["pattern"] == 28
    assert full["contributions"]["technical"] == 12
    missing = score_of(
        {
            "similarity_score": 80,
            "technical_score": 60,
            "volume_score": 60,
            "capital_score": None,
            "history_limit_up_rate": 60,
            "risk_score": 0,
        }
    )
    assert missing["weight_total"] == 0.85
    assert abs(missing["weighted_score"] - round(58 / 0.85, 2)) < 0.02


def test_risk_and_level_edges_and_empty_is_not_zero():
    assert risk_penalty(80) == 20
    assert risk_penalty(79.99) == 10
    assert risk_penalty(60) == 10
    assert risk_penalty(59.99) == 5
    assert risk_penalty(40) == 5
    assert risk_penalty(39.99) == 0
    assert risk_penalty(None) == 0
    empty = score_of({})
    assert empty["composite_score"] == 0
    assert empty["has_signal"] is False
    from backend.app.services.score_model import read_composite, read_gap, read_level

    assert read_composite(empty) is None
    assert read_level(empty) == ""
    assert read_gap(empty) is None


def test_capital_stays_empty_even_when_amount_is_present():
    assert capital_score_of({"amount": 1e9, "turnover_rate": 3, "volume": 100}) is None
    assert pattern_match_rate(0, 10) == 0
    assert pattern_match_rate(1, 0) is None


def test_formula_text_mentions_only_the_fixed_weights():
    import re

    found = set(re.findall(r"\d+%", FORMULA_TEXT))
    assert found == {"35%", "20%", "15%"}
    assert "不要重新计算" in PROMPT_RUBRIC
    for text in PUBLIC_TEXTS:
        assert "*" not in text
        assert "`" not in text
    assert WEIGHT_PATTERN == 0.35
    assert WEIGHT_TECHNICAL == 0.20
    assert WEIGHT_VOLUME == WEIGHT_CAPITAL == WEIGHT_HISTORY == 0.15


def _calendar(start: date, end: date) -> list[date]:
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days
