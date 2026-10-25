"""档3（前半）：单日特征、技术指标与训练宽表。

这一档的硬规矩只有一条——**只用已经发生的行情**，所以判据全部围绕它设计：

* 特征与指标各有一条「截尾不变」：把未来那些天砍掉再算一遍，已经发生的那些天必须
  一个字都不动。同一条判据里再做反向对照——只喂那一天之前的线，出来的数必须**与**喂
  全长历史时不同，否则「不变」只是量具坏了。
* 乱序行情直接报错，不静默算出错值。
* 命中 error 级质量异常的训练行**照样写**、但标记拦下：`matrix()` 默认不取它。

读库一律在会话里把值折成普通 dict/标量再断言：`session_scope()` 提交后对象就过期了，
拿着过期对象断言会得到一个和判据无关的异常。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from seeds import save_stock, seed, series, weekdays

from backend.app.algorithms.features import (
    AGGREGATE_NAMES,
    DAILY_SPEC,
    VECTOR_DIM,
    Bar,
    build_three_day_window,
    compute_day_features,
    feature_names,
)
from backend.app.algorithms.indicators import (
    MIN_BARS_FOR_INDICATORS,
    compute_indicator_series,
    indicator_feature_names,
    indicators_at,
    is_valid_bar,
)
from backend.app.core.database import session_scope
from backend.app.models.tables import (
    CalcBar,
    DailyFeature,
    DailyQuote,
    DataQualityError,
    Stock,
    StockTrainingRow,
)
from backend.app.services import feature_service, training_service
from backend.app.services.history_backfill import load_bars
from backend.app.services.quality_service import ERROR


def _read(code: str, as_of: date) -> list[Bar]:
    with session_scope() as session:
        return load_bars(session, code, as_of)


def _training_row(code: str, label_day: date) -> dict:
    with session_scope() as session:
        row = (
            session.query(StockTrainingRow)
            .filter(StockTrainingRow.stock_code == code, StockTrainingRow.label_date == label_day)
            .one()
        )
        return {
            "cutoff": row.cutoff_date,
            "passed": bool(row.quality_passed),
            "reason": row.reason,
            "label_limit_up": bool(row.label_limit_up),
            "features": dict(row.features),
        }


def _quote_values(code: str, day: date) -> dict:
    with session_scope() as session:
        row = (
            session.query(DailyQuote)
            .filter(DailyQuote.stock_code == code, DailyQuote.trade_date == day)
            .one()
        )
        return {
            "close": float(row.close),
            "volume": float(row.volume),
            "limit_up": bool(row.is_limit_up),
            "change_percent": row.change_percent,
        }


# --- 单日特征：只用已经发生的行情 ---------------------------------------------


def test_day_features_are_truncation_invariant(client):
    """第 i 天的特征只由 `bars[: i + 1]` 决定；喂全长历史算出的是同一个值。"""
    days = seed()
    bars = _read("600000", days[-1])
    assert len(bars) == len(days)
    batch = dict(feature_service.features_for_range(bars, days[0], days[-1]))

    for index in (2, 5, len(days) - 2):
        by_hand = compute_day_features(list(bars[: index + 1]))
        assert batch[days[index]] == by_hand, f"{days[index]} 的特征与「只看那一天之前」算出来的不一致"

    # 反向对照：同一个「今天」，前置历史深浅不同就必须给出不同的数——
    # 否则上面那条恒等只是量具坏了（`position_20`、`volume_ratio_5` 都吃历史长度）。
    same_day_deep = compute_day_features(list(bars[:7]))
    same_day_shallow = compute_day_features(list(bars[3:7]))
    assert same_day_deep["trade_date"] == same_day_shallow["trade_date"]
    assert same_day_deep != same_day_shallow, "同一天的特征与前置历史长度无关，说明窗口没在推进"


def test_features_for_range_only_covers_the_requested_days(client):
    """区间外的一天不进结果；半天线（`is_partial`）不当那天的收盘特征。"""
    days = seed()
    bars = _read("600000", days[-1])
    inside = feature_service.features_for_range(bars, days[4], days[6])
    assert [day for day, _features in inside] == [days[4], days[5], days[6]]
    assert all(features["trade_date"] == day.isoformat() for day, features in inside)

    partial = [
        Bar(
            trade_date=bar.trade_date,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            pre_close=bar.pre_close,
            volume=bar.volume,
            amount=bar.amount,
            turnover_rate=bar.turnover_rate,
            is_partial=index == len(bars) - 1,
        )
        for index, bar in enumerate(bars)
    ]
    assert [day for day, _features in feature_service.features_for_range(partial, days[0], days[-1])] == days[:-1]


def test_vector_shape_is_pinned(client):
    """67 维 = 20 项 × 三天 + 7 项聚合。维度漂了，样本、匹配、聚类三张表一起作废。"""
    assert VECTOR_DIM == len(DAILY_SPEC) * 3 + len(AGGREGATE_NAMES) == 67
    assert len(feature_names()) == VECTOR_DIM
    assert feature_names()[0] == "t3_open_change"
    assert feature_names()[-1] == AGGREGATE_NAMES[-1]

    days = seed()
    window = build_three_day_window(_read("600000", days[-1]), days[-1], include_as_of_date=True)
    assert window.ok and len(window.vector) == VECTOR_DIM


# --- 三日窗口：晚于评估日的行情一律丢掉 ---------------------------------------


def test_three_day_window_drops_every_bar_after_the_as_of_day(client):
    """评估日之后的线不参与建窗口，也不参与打分。"""
    days = seed()
    as_of = days[-1]
    bars = _read("600000", as_of)
    spike = Bar(
        trade_date=as_of + timedelta(days=1),
        open=1.0,
        high=999.0,
        low=0.5,
        close=999.0,
        pre_close=9.9,
        volume=9_999_999,
        amount=99_999_999,
        turnover_rate=88.0,
    )
    clean = build_three_day_window(bars, as_of, include_as_of_date=True)
    with_future = build_three_day_window([*bars, spike], as_of, include_as_of_date=True)
    assert with_future.ok and with_future.vector == clean.vector
    assert [bar.trade_date for bar in with_future.days] == [bar.trade_date for bar in clean.days]

    # 样本侧再早一天：涨停当天只贡献标签，绝不进向量。
    sample = build_three_day_window([*bars, spike], days[-2], include_as_of_date=False)
    assert sample.ok
    assert max(bar.trade_date for bar in sample.days) < days[-2]
    assert [bar.trade_date for bar in sample.days] == days[-5:-2]
    assert sample.vector != clean.vector


def test_window_that_cannot_be_built_reports_a_reason(client):
    """凑不齐三天是「算不了」：回一条原因，不交一个补过零的向量。"""
    days = seed()
    bars = _read("600000", days[-1])
    for keep in (0, 1, 2):
        as_of = days[-1] if keep == 0 else days[keep - 1]
        result = build_three_day_window(bars[:keep], as_of, include_as_of_date=True)
        assert result.ok is False
        assert result.reason == "insufficient_window"
        assert result.vector == [] and result.days == []


# --- 技术指标：递推不回头看未来，坏线不拖平均线 --------------------------------


def test_indicator_series_is_the_same_whenever_future_bars_are_cut(client):
    """cutoff 那天的指标 = 只看 cutoff 之前（含）的线算出来的指标。"""
    days = seed(count=MIN_BARS_FOR_INDICATORS + 12)
    bars = _read("600000", days[-1])
    produced = compute_indicator_series(bars)
    assert len(produced) == len(bars)
    for cut in (MIN_BARS_FOR_INDICATORS, len(bars) - 3, len(bars) - 1):
        assert compute_indicator_series(bars[:cut])[-1] == produced[cut - 1], f"第 {cut} 根的指标变了"

    # 反向对照：追加一根暴涨的线，最后一根的指标必须动——
    # 不然「截尾不变」可能只是引擎压根没在算。
    last = bars[-1]
    spike = Bar(
        trade_date=last.trade_date + timedelta(days=3),
        open=last.close,
        high=float(last.close) * 1.2,
        low=last.close,
        close=float(last.close) * 1.19,
        pre_close=last.close,
        volume=last.volume * 3,
        amount=last.amount * 3,
        turnover_rate=last.turnover_rate,
    )
    extended = compute_indicator_series([*bars, spike])
    assert extended[-1]["ma5"] != produced[-1]["ma5"], "新来的一根没进引擎，上面那条不变是假的"
    assert extended[-2] == produced[-1], "新来的一根把已经过去的那天也改了，这是前视"


def test_indicator_series_refuses_unsorted_bars(client):
    """乱序直接报错：均线与 Wilder 递推吃的是顺序，不是日期。"""
    days = seed(count=MIN_BARS_FOR_INDICATORS + 5)
    bars = _read("600000", days[-1])
    shuffled = [*bars[:3], bars[5], *bars[3:5], *bars[6:]]
    with pytest.raises(ValueError) as exc:
        compute_indicator_series(shuffled)
    assert "升序" in str(exc.value) and "前视" in str(exc.value)
    assert compute_indicator_series(bars)[-1]["ma5"] is not None  # 正向对照：排好就算得出来


@pytest.mark.parametrize(
    "broken",
    [
        {"close": None},
        {"open": None},
        {"close": 0.0},
        {"high": 1.0, "low": 9.0},
        {"high": 8.0, "close": 9.5},
        {"close": 10.5},
        {"low": 9.7},
    ],
)
def test_self_contradicting_bars_are_rejected(broken):
    """缺价、非正、high<low、收盘越出高低界——这些线不进任何窗口与递推。"""
    base = dict(
        trade_date=date(2026, 6, 12),
        open=9.6,
        high=10.0,
        low=9.0,
        close=9.5,
        pre_close=9.4,
        volume=1000,
        amount=9500,
        turnover_rate=1.5,
    )
    assert is_valid_bar(Bar(**{**base, **broken})) is False, broken
    assert is_valid_bar(Bar(**base)) is True  # 正向对照：上面那些改动确实改坏了


def test_a_suspended_bar_does_not_drag_the_moving_average(client):
    """坏线自身全 None，且不推进递推、不改它之前那些天的指标。"""
    days = seed(count=MIN_BARS_FOR_INDICATORS + 5)
    bars = _read("600000", days[-1])
    good = compute_indicator_series(bars)
    broken = list(bars)
    broken[-1] = Bar(
        trade_date=broken[-1].trade_date,
        open=None,
        high=None,
        low=None,
        close=None,
        pre_close=broken[-2].close,
        volume=0,
        amount=0,
    )
    result = compute_indicator_series(broken)
    assert all(value is None for value in result[-1].values())
    assert result[:-1] == good[:-1], "一根坏线改掉了它之前那些天的指标"
    assert good[-1]["ma5"] is not None
    assert indicators_at([]) == {name: None for name in indicator_feature_names()}


# --- 训练宽表：T 日只许看 T-1 及更早 ------------------------------------------


def test_column_names_are_the_documented_seventy_four(client):
    names = training_service.column_names()
    expected = [
        f"t{lag}_{field}" for lag in training_service.BAR_LAGS for field in training_service.BAR_FIELDS
    ]
    expected += [f"cutoff_{field}" for field in training_service.CUTOFF_DERIVED_FIELDS]
    expected += list(indicator_feature_names())
    assert names == expected and len(names) == 74


def test_cutoff_derived_columns_all_come_from_a_real_feature_row(client):
    """`CUTOFF_DERIVED_FIELDS` 写的名字必须在 `compute_day_features()` 真产出的键里。"""
    days = seed()
    features = compute_day_features(_read("600000", days[-1]))
    assert [name for name in training_service.CUTOFF_DERIVED_FIELDS if name not in features] == []


def test_training_row_features_stop_at_the_cutoff_day(client):
    """`t1_*…t5_*` 是 T-1…T-5 那五根线；T 日只贡献标签列。"""
    days = seed()
    code, label_day = "600000", days[6]
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", label_day, label_day)
        training_service.build_training(session, label_day, label_day, codes=[code], stats=stats)
    row = _training_row(code, label_day)
    lags = training_service.prior_lag_dates(days, label_day)
    assert row["cutoff"] == lags[1]
    assert lags[5] == days[1] and set(lags) == set(training_service.BAR_LAGS)

    quotes = {day: _quote_values(code, day) for day in lags.values()}
    today = _quote_values(code, label_day)
    for lag, day in lags.items():
        assert row["features"][f"t{lag}_close"] == pytest.approx(quotes[day]["close"])
        assert row["features"][f"t{lag}_volume"] == pytest.approx(quotes[day]["volume"])
        # 这段行情严格上行，任何一列等于 T 日收盘都说明 cutoff 写错成了 label_day。
        assert row["features"][f"t{lag}_close"] != pytest.approx(today["close"])
    assert row["features"]["label_close"] == pytest.approx(today["close"])
    assert row["features"]["label_change_percent"] == pytest.approx(today["change_percent"])
    assert row["label_limit_up"] == today["limit_up"]


def test_mutating_the_label_day_moves_only_the_labels(client):
    """注入违规的负向控制：改 T 日的收盘，重算之后只有 `label_*` 变。"""
    days = seed()
    code, label_day = "600001", days[7]
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", label_day, label_day)
        training_service.build_training(session, label_day, label_day, codes=[code], stats=stats)
    before = _training_row(code, label_day)["features"]

    with session_scope() as session:
        quote = (
            session.query(DailyQuote)
            .filter(DailyQuote.stock_code == code, DailyQuote.trade_date == label_day)
            .one()
        )
        quote.close = float(quote.close) * 1.05
        quote.volume = 777_777
        quote.amount = 8_888_888

    with session_scope() as session:
        stats = feature_service.new_stats("build_training", label_day, label_day)
        training_service.build_training(session, label_day, label_day, codes=[code], stats=stats)
    after = _training_row(code, label_day)["features"]

    changed = {key for key in after if after[key] != before.get(key)}
    assert changed and all(key.startswith("label_") for key in changed), changed
    assert after["label_close"] != before["label_close"], "改了 T 日的收盘，标签列却纹丝不动"


def test_training_blocked_row_is_written_and_flagged(client):
    """error 级异常：行照写、`quality_passed=False` 并记下原因；`matrix()` 默认不取它。"""
    days = seed()
    code = "000002"
    label_day = days[8]
    dirty = sorted(day for day in days if day < label_day)[-1]
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", days[0], days[-1])
        training_service.build_training(session, days[0], days[-1], codes=[code], stats=stats)
        clean = training_service.matrix(session, days[0], days[-1])
        clean_counts = training_service.training_counts(session)
    assert clean["total"] == stats["training_count"] > 0
    assert clean_counts["blocked_rows"] == 0, "没注入脏线之前就有拦下的行，下面的对照没意义"

    with session_scope() as session:
        session.add(
            DataQualityError(
                stock_code=code,
                trade_date=dirty,
                check_name="change_percent_check",
                severity=ERROR,
                detail="测试注入",
            )
        )

    with session_scope() as session:
        stats = feature_service.new_stats("build_training", days[0], days[-1])
        training_service.build_training(session, days[0], days[-1], codes=[code], stats=stats)
        passed_only = training_service.matrix(session, days[0], days[-1])
        everything = training_service.matrix(session, days[0], days[-1], only_passed=False)
        counts = training_service.training_counts(session)

    assert _training_row(code, label_day)["passed"] is False
    assert _training_row(code, label_day)["reason"] == training_service.BLOCK_QUALITY_ERROR
    # 脏线日之前那几天看不见它：闸门只挡够得着的日子，不是一棍子打死整只票。
    assert _training_row(code, days[5])["passed"] is True
    assert counts["blocked_rows"] == stats["training_blocked"] > 0
    assert everything["total"] == counts["rows"] == clean["total"]
    assert passed_only["total"] == counts["rows"] - counts["blocked_rows"]
    assert training_service.BLOCK_QUALITY_ERROR in counts["reason_labels"]


def test_training_needs_five_prior_bars_and_says_so(client):
    """全市场前五天都不够的日子：不出行，只记一笔日级账（代码留空），不给每只票各记一笔。"""
    days = seed()
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", days[0], days[3])
        training_service.build_training(session, days[0], days[3], stats=stats)
        rows = session.query(StockTrainingRow).filter(StockTrainingRow.label_date <= days[3]).count()
        page = feature_service.skip_page(session, stage="training")
    assert rows == 0
    assert stats["skipped"] == {training_service.SKIP_MARKET_HISTORY: 4}, stats["skipped"]
    assert page["total"] == 4, "四只票四天该是四笔日级账，不是十六笔逐只账"
    assert {item["stock_code"] for item in page["items"]} == {""}
    assert sorted(item["trade_date"] for item in page["items"]) == [day.isoformat() for day in days[:4]]
    assert str(training_service.REQUIRED_PRIOR_BARS) in page["items"][0]["detail"]


def test_a_suspended_stock_is_skipped_on_its_own(client):
    """全市场够五天、只有这只票缺线（停牌/新上市）：才按只记账，别人照常出行。"""
    days = seed()
    late = days[3:]
    save_stock("600002", "新上市", "SSE", series(late, limit_on=set(), start=5.0))
    label_day = days[6]
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", label_day, label_day)
        training_service.build_training(session, label_day, label_day, stats=stats)
        page = feature_service.skip_page(session, stage="training")
        written = {
            row.stock_code
            for row in session.query(StockTrainingRow).filter(StockTrainingRow.label_date == label_day)
        }
    assert "600002" not in written and len(written) == 4
    assert page["by_reason"] == {training_service.SKIP_INSUFFICIENT_HISTORY: 1}
    assert page["items"][0]["stock_code"] == "600002"


def test_backfilled_history_does_not_make_a_limit_up_only_training_set(client):
    """补拉进 `calc_bars` 的历史只给涨停票有：它不能替那几只票凑出全市场都凑不满的 T-5。"""
    days = seed()
    retained = days[-4:]
    with session_scope() as session:
        session.query(DailyQuote).filter(DailyQuote.trade_date < retained[0]).delete()
    # 只给 600000 补一段更早的线，就像它涨停后被补拉过历史那样。
    with session_scope() as session:
        for offset in range(1, 8):
            day = retained[0] - timedelta(days=offset * 7)
            session.add(
                CalcBar(
                    stock_code="600000",
                    trade_date=day,
                    open=9.0,
                    high=9.2,
                    low=8.9,
                    close=9.1,
                    pre_close=9.0,
                    volume=1000.0,
                    amount=9100.0,
                    turnover_rate=1.0,
                )
            )
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", retained[0], retained[-1])
        training_service.build_training(session, retained[0], retained[-1], stats=stats)
        rows = session.query(StockTrainingRow).count()
    assert rows == 0, "只有补过历史的那只票出了训练行：训练集成了涨停票专场"
    assert stats["skipped"] == {training_service.SKIP_MARKET_HISTORY: len(retained)}


def test_rebuilding_training_replaces_the_old_per_stock_ledger(client):
    """旧口径留下的逐只「只有 4 根」账，重建这段区间时要被这一轮的结果换掉。"""
    days = seed()
    with session_scope() as session:
        for code in ("600000", "600001", "000001", "000002"):
            feature_service.record_skip(
                session,
                day=days[2],
                stock_code=code,
                stage="training",
                reason=training_service.SKIP_INSUFFICIENT_HISTORY,
                detail="标签日之前只有 2 根，需要 5 根",
            )
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", days[0], days[-1])
        training_service.build_training(session, days[0], days[-1], stats=stats)
        page = feature_service.skip_page(session, stage="training", day=days[2])
    assert page["by_reason"] == {training_service.SKIP_MARKET_HISTORY: 1}, page["by_reason"]


def test_prior_lag_dates_refuses_the_label_day_and_pads_nothing(client):
    """`day < label_day` 在这里再切一次：调用方把 T 日混进 ordered 也带不进特征。"""
    days = weekdays(12)
    label = days[6]
    lags = training_service.prior_lag_dates([*days, days[-1] + timedelta(days=1)], label)
    assert set(lags) == set(training_service.BAR_LAGS)
    assert max(lags.values()) < label
    assert lags[1] == days[5] and lags[5] == days[1]

    thin = training_service.prior_lag_dates(days[:4], days[5])
    assert set(thin) == {1, 2, 3, 4}, "取不满 T-5 时缺的那一格不许补一个日期上来"


def test_cutoff_features_are_null_until_the_feature_table_is_built(client):
    """训练宽表的 `cutoff_*` 吃 `daily_features`：没重建特征时是 null，不是 0。"""
    days = seed()
    code, label_day = "600000", days[9]
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", label_day, label_day)
        training_service.build_training(session, label_day, label_day, codes=[code], stats=stats)
    blank = {name: _training_row(code, label_day)["features"][f"cutoff_{name}"]
             for name in training_service.CUTOFF_DERIVED_FIELDS}
    assert all(value is None for value in blank.values()), blank

    with session_scope() as session:
        rebuild = feature_service.new_stats("rebuild_features", days[0], days[-1])
        feature_service.rebuild_features(session, days[0], days[-1], codes=[code], stats=rebuild)
    assert rebuild["feature_count"] == len(days)
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", label_day, label_day)
        training_service.build_training(session, label_day, label_day, codes=[code], stats=stats)
        row = (
            session.query(StockTrainingRow)
            .filter(StockTrainingRow.stock_code == code, StockTrainingRow.label_date == label_day)
            .one()
        )
        stored = (
            session.query(DailyFeature)
            .filter(DailyFeature.stock_code == code, DailyFeature.trade_date == row.cutoff_date)
            .one()
        )
        features = dict(row.features)
        stored_features = dict(stored.features)
    for name in training_service.CUTOFF_DERIVED_FIELDS:
        assert features[f"cutoff_{name}"] == stored_features[name]
    assert features["cutoff_close_change"] is not None
    assert set(feature_service.REQUIRED_FEATURE_KEYS) <= set(stored_features)


def test_rebuild_features_is_idempotent_and_warms_up_at_the_range_start(client):
    """重跑不产生第二行；预热锚在区间首日，不是今天。"""
    days = seed()
    low, high = days[4], days[-1]
    with session_scope() as session:
        warmup = feature_service.history_start_for(session, low, feature_service.HISTORY_LOOKBACK_TRADING_DAYS)
    assert warmup == days[0], "库里只有 12 天，区间首日的预热只能取到最早那天"

    with session_scope() as session:
        first = feature_service.new_stats("rebuild_features", low, high)
        feature_service.rebuild_features(session, low, high, stats=first)
    with session_scope() as session:
        second = feature_service.new_stats("rebuild_features", low, high)
        feature_service.rebuild_features(session, low, high, stats=second)
        rows = list(session.query(DailyFeature).all())
        payloads = {(row.stock_code, row.trade_date): dict(row.features) for row in rows}

    assert first["warmup_from"] == second["warmup_from"] == warmup.isoformat()
    # 记账数的是「写进多少行」：四只票 × 区间里的 8 个交易日。
    assert first["feature_count"] == second["feature_count"] == 4 * (len(days) - 4)
    assert first["universe_count"] == second["universe_count"] == 4
    assert first["date_range"] == [day.isoformat() for day in days[4:]]
    assert len(payloads) == first["feature_count"], "重跑一次就多出一批行"
    assert {day for _code, day in payloads} == set(days[4:])
    assert all(set(feature_service.REQUIRED_FEATURE_KEYS) <= set(value) for value in payloads.values())


def test_rebuild_features_records_the_codes_it_could_not_find(client):
    """指定代码里查无此票的：记 `no_bar` 跳过账，而不是安静地少算。"""
    days = seed()
    with session_scope() as session:
        stats = feature_service.new_stats("rebuild_features", days[0], days[-1])
        feature_service.rebuild_features(session, days[0], days[-1], codes=["600000", "999999"], stats=stats)
        page = feature_service.skip_page(session, stage="feature", reason=feature_service.SKIP_NO_BAR)
    assert stats["skipped"][feature_service.SKIP_NO_BAR] == 1
    assert stats["universe_count"] == 1 and stats["stock_count"] == 1
    assert stats["feature_count"] == len(days)
    assert page["total"] == 1 and page["items"][0]["stock_code"] == "999999"


def test_repeated_skips_do_not_inflate_the_ledger(client):
    """同一只股票同一天同一原因只留一条：重跑三遍，跳过账还是那一条。"""
    days = seed()
    for _round in range(3):
        with session_scope() as session:
            stats = feature_service.new_stats("rebuild_features", days[0], days[-1])
            feature_service.rebuild_features(session, days[0], days[-1], codes=["999999"], stats=stats)
    assert stats["universe_count"] == 0
    with session_scope() as session:
        page = feature_service.skip_page(session, stage="feature", reason=feature_service.SKIP_NO_BAR)
    assert page["total"] == 1, "跳过账被重跑刷成了三条，页面就数不清到底缺了什么"


def test_training_is_idempotent_row_for_row(client):
    """训练行按 (代码, 标签日) upsert：重跑一遍，行数与每格的值都不动。"""
    days = seed()
    code = "600000"
    with session_scope() as session:
        rebuild = feature_service.new_stats("rebuild_features", days[0], days[-1])
        feature_service.rebuild_features(session, days[0], days[-1], codes=[code], stats=rebuild)
        stats = feature_service.new_stats("build_training", days[0], days[-1])
        training_service.build_training(session, days[0], days[-1], codes=[code], stats=stats)
        once = {
            row.label_date: dict(row.features)
            for row in session.query(StockTrainingRow).filter(StockTrainingRow.stock_code == code)
        }
        first_count = stats["training_count"]
    with session_scope() as session:
        stats = feature_service.new_stats("build_training", days[0], days[-1])
        training_service.build_training(session, days[0], days[-1], codes=[code], stats=stats)
        twice = {
            row.label_date: dict(row.features)
            for row in session.query(StockTrainingRow).filter(StockTrainingRow.stock_code == code)
        }
    assert stats["training_count"] == first_count == len(twice)
    assert once == twice
    # 只有攒够五根前置线的那些天才出行：区间前五天出不来。
    assert min(once) == days[training_service.REQUIRED_PRIOR_BARS]


def test_matrix_columns_line_up_with_the_stored_rows(client):
    """`matrix()` 的表头与每列取值来自同一份 `column_names()`。"""
    days = seed()
    code = "600001"
    with session_scope() as session:
        rebuild = feature_service.new_stats("rebuild_features", days[0], days[-1])
        feature_service.rebuild_features(session, days[0], days[-1], codes=[code], stats=rebuild)
        stats = feature_service.new_stats("build_training", days[0], days[-1])
        training_service.build_training(session, days[0], days[-1], codes=[code], stats=stats)
        page = training_service.matrix(session, days[0], days[-1], codes=[code])
        stored = [
            {
                "label": row.label_date.isoformat(),
                "cutoff": row.cutoff_date.isoformat(),
                "features": dict(row.features),
            }
            for row in session.query(StockTrainingRow).filter(StockTrainingRow.stock_code == code)
        ]
    assert page["columns"][:5] == ["stock_code", "label_date", "cutoff_date", "label_limit_up", "quality_passed"]
    assert len(page["columns"]) == 5 + len(training_service.column_names())
    assert page["total"] == len(stored) == stats["training_count"]
    by_day = {item["label"]: item for item in stored}
    for line in page["rows"]:
        row = by_day[line[1]]
        assert line[2] == row["cutoff"]
        for name, value in zip(training_service.column_names(), line[5:]):
            assert value == row["features"].get(name), (line[1], name)
    assert "不是对未来收益的预测" in page["disclaimer"]


def test_training_skips_a_code_that_left_the_master_table(client):
    """行情在、主档没有的那只票：记 `code_not_in_master`，不硬造一行训练数据。"""
    days = seed()
    save_stock("600009", "临时行情", "SSE", series(days, limit_on=set(), start=5.0))
    with session_scope() as session:
        session.query(Stock).filter(Stock.code == "600009").delete()
        stats = feature_service.new_stats("build_training", days[0], days[-1])
        training_service.build_training(session, days[0], days[-1], codes=["600009"], stats=stats)
        rows = session.query(StockTrainingRow).filter(StockTrainingRow.stock_code == "600009").count()
    assert rows == 0
    assert stats["skipped"][training_service.SKIP_NOT_IN_MASTER] == 1
