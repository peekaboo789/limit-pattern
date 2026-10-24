"""档2：回测账、校准、榜上概率列、次日预测池。

判据一律配**注入违规的负向控制**：只验「跑出来是对的」测不出闸门是死的，
所以防泄漏那五条各造一条坏数据、连板闸门各造一个「次日正好是基准日」的样本，
并且先证明注入真的生效（假 kline 源先自答一次），再证明它一次都没被敲响。
"""

import time
from datetime import date, datetime, timedelta

import pytest

from backend.app.algorithms.calibration import (
    brier_score,
    calibration_curve,
    expected_calibration_error,
    log_loss,
)
from backend.app.core.constants import MIN_BAND_OBSERVATIONS
from backend.app.core.database import session_scope
from backend.app.models.tables import (
    BacktestObservation,
    BacktestRun,
    DailyQuote,
    DataQualityError,
    Stock,
    TradingDay,
)
from backend.app.services import ai_evidence
from backend.app.services import backtest_service
from backend.app.services import prediction_service
from backend.app.services import realtime_service
from backend.app.services import realtime_tracker
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.limit_up_service import annotate_history, limit_up_price, ratio_for
from backend.app.services.quality_service import ERROR, error_pairs
from backend.app.services.sync_service import fetch_kline_retry, set_fetcher


def weekdays(count: int, end: date = date(2026, 6, 12)) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _series(days: list[date], *, limit_on: set[int], start: float = 10.0) -> list[dict]:
    price = start
    rows = []
    for index, day in enumerate(days):
        pre = round(price, 2)
        if index in limit_on:
            close = float(limit_up_price(pre, ratio_for("main_board", False, day)))
            high = close
            low = pre
        else:
            close = round(pre * (1.005 + 0.002 * (index % 3)), 2)
            high = round(close * 1.004, 2)
            low = pre
        rows.append(
            {
                "trade_date": day,
                "open": pre,
                "high": high,
                "low": low,
                "close": close,
                "pre_close": pre,
                "volume": 1000 + index * 137,
                "amount": 10000 + index * 91,
                "turnover_rate": 1.2 + index / 10,
            }
        )
        price = close
    return rows


def _save(code: str, name: str, exchange: str, rows: list[dict]) -> None:
    board = "main_board"
    labelled = annotate_history(rows, board=board, is_st=False, list_date=None, trading_days=[row["trade_date"] for row in rows])
    with session_scope() as session:
        stock = session.get(Stock, code)
        if stock is None:
            stock = Stock(code=code)
            session.add(stock)
        stock.name = name
        stock.exchange = exchange
        stock.board = board
        for item in labelled:
            session.add(
                DailyQuote(
                    stock_code=code,
                    trade_date=item["trade_date"],
                    open=item["open"],
                    high=item["high"],
                    low=item["low"],
                    close=item["close"],
                    pre_close=item["pre_close"],
                    volume=item["volume"],
                    amount=item["amount"],
                    turnover_rate=item["turnover_rate"],
                    change_percent=item.get("change_percent"),
                    is_limit_up=item["is_limit_up"],
                    is_limit_down=item["is_limit_down"],
                    limit_up_price=item["limit_up_price"],
                    limit_down_price=item["limit_down_price"],
                    limit_up_percent=item["limit_up_percent"],
                    limit_up_reason=item["limit_up_reason"],
                    source="test",
                )
            )
            if session.get(TradingDay, item["trade_date"]) is None:
                session.add(TradingDay(trade_date=item["trade_date"], is_open=True, limit_up_count=0))


def _seed(count: int = 12) -> list[date]:
    """四只票、三种涨停分布：池子有样本、候选有得比、最后一天必然还没揭晓。"""
    days = weekdays(count)
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={4, 7, 9}))
    _save("600001", "邯郸钢铁", "SSE", _series(days, limit_on={5, 8}, start=6.0))
    _save("000001", "平安银行", "SZSE", _series(days, limit_on=set(), start=8.0))
    _save("000002", "万科A", "SZSE", _series(days, limit_on={6}, start=14.0))
    return days


@pytest.fixture()
def no_backfill():
    """装一个会答话的假 kline 源：既证明注入生效，又让「一次都没补拉」成为可数的判据。"""
    calls: list[tuple[str, date, date]] = []

    def fake(code: str, start: date, end: date) -> list[dict]:
        calls.append((code, start, end))
        return []

    set_fetcher(kline_fn=fake)
    assert fetch_kline_retry("600000", date(2026, 6, 1), date(2026, 6, 2)) == []
    assert calls, "假行情源没被敲响，注入本身没生效，后面那条零补拉判据就是假的"
    calls.clear()
    yield calls
    set_fetcher()


# --- 校准那三个数 -------------------------------------------------------------


def test_calibration_metrics_return_none_not_zero_for_empty_input():
    assert brier_score([], []) is None
    assert log_loss([], []) is None
    assert expected_calibration_error([], []) is None
    assert calibration_curve([], []) == []


def test_calibration_metrics_reject_length_mismatch():
    with pytest.raises(ValueError) as exc:
        brier_score([0.1, 0.2], [1])
    assert "长度不一致" in str(exc.value)
    with pytest.raises(ValueError):
        calibration_curve([0.1], [])


def test_calibration_metrics_on_perfect_and_biased_predictions():
    assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == pytest.approx(0.0)
    assert log_loss([1.0, 0.0], [1, 0]) is not None and log_loss([1.0, 0.0], [1, 0]) < 1e-5
    assert expected_calibration_error([1.0, 0.0], [1, 0]) == pytest.approx(0.0, abs=1e-6)
    # p=1.0 是合法预测：最后一箱必须右闭，否则这一条掉在 bins 之外。
    curve = calibration_curve([1.0, 1.0], [1, 0], bins=5)
    assert curve[-1]["upper"] >= 1.0
    assert curve[-1]["count"] == 2
    assert curve[-1]["mean_predicted"] == pytest.approx(1.0)
    assert curve[-1]["observed_rate"] == pytest.approx(0.5)
    # 只输出非空段：全 0.2 的预测只占一格，其余四格不该以 count=0 混进来。
    assert sum(bin_["count"] for bin_ in calibration_curve([0.2] * 4, [0, 0, 1, 1], bins=10)) == 4
    biased = expected_calibration_error([0.9, 0.9], [0, 0])
    assert biased is not None and biased > 0.5


# --- 回测主流程 ---------------------------------------------------------------


def test_run_backtest_is_auditable_and_leakage_gate_passes(client, no_backfill):
    days = _seed()
    with session_scope() as session:
        payload = backtest_service.run_backtest(session, start=days[0], end=days[-1])
        run_id = backtest_service.persist_run(session, payload)
    assert payload["day_count"] == len(days)
    assert payload["observations"], "一天观测都没有，后面的分档全是编的"
    assert payload["leakage"]["passed"] is True, payload["leakage"]["detail"]
    assert payload["baseline_rate"] is not None, "区间里除最后一天都该有揭晓的观测"
    # 不补拉这条判据：假源装了却一次都没被敲，说明样本池只吃库里已有的线。
    assert no_backfill == []

    for item in payload["observations"]:
        assert item["window"][-1] <= item["trade_date"]
        if item["next_date"]:
            assert item["window"][-1] < item["next_date"]
            assert item["next_date"] > item["trade_date"]
        else:
            assert item["next_limit_up"] is None
        # 候选侧剔了当日涨停：这一行明天才见分晓，昨天它还没涨停。
        with session_scope() as session:
            row = (
                session.query(DailyQuote.is_limit_up)
                .filter(DailyQuote.stock_code == item["stock_code"], DailyQuote.trade_date == item["trade_date"])
                .one_or_none()
            )
        assert row is None or row[0] is False, f"{item['stock_code']} 当天已涨停，不该进观测"

    last_day = days[-1].isoformat()
    assert all(item["next_limit_up"] is None for item in payload["observations"] if item["trade_date"] == last_day)
    assert sum(bucket["pending_count"] for bucket in payload["buckets"]) > 0

    assert all(bucket["band"] in backtest_service.band_labels() for bucket in payload["buckets"])
    assert len(payload["buckets"]) == len(backtest_service.band_labels())

    with session_scope() as session:
        run = session.get(BacktestRun, run_id)
        assert run.status == "done"
        assert run.finished_at is not None
        assert run.note and "个交易日" in run.note
        assert run.parameters["compare_window_trading_days"] == 3
        assert run.observation_count == len(payload["observations"])
        stored = session.query(BacktestObservation).filter(BacktestObservation.run_id == run_id).count()
        assert stored == len(payload["observations"])


def test_backtest_range_validation(client):
    days = _seed()
    with session_scope() as session:
        with pytest.raises(ValueError) as reversed_range:
            backtest_service.run_backtest(session, start=days[-1], end=days[0])
        assert "晚于结束日" in str(reversed_range.value)
        with pytest.raises(ValueError):
            backtest_service.run_backtest(session, start=days[0], end=days[-1], split_ratio=1.5)
        with pytest.raises(LookupError):
            backtest_service.run_backtest(
                session, start=days[0] - timedelta(days=200), end=days[0] - timedelta(days=190)
            )


def test_leakage_gate_flags_each_injected_violation(client, no_backfill):
    """五条判据逐条灌一条坏数据：闸门要是死的，这里就该绿得可疑。"""
    days = _seed()
    with session_scope() as session:
        payload = backtest_service.run_backtest(session, start=days[0], end=days[-1])
    # 正向对照用真跑出来的一条观测：造出来的行在空库里连抽验都重算不了，
    # 绿不绿取决于库里有没有那根线，而不是闸门。
    good_obs = next(item for item in payload["observations"] if item["next_date"])
    good_rows = [next(row for row in payload["days"] if row["trade_date"] == good_obs["trade_date"])]
    assert good_rows[0]["pool_size"] > 0 and good_rows[0]["scanned_count"] > 0
    with session_scope() as session:
        report = backtest_service._leakage(good_rows, [dict(good_obs)], session)
    assert report["passed"] is True, report["detail"]

    late_window = dict(good_obs, window=["2026-06-09", "2026-06-10", "2026-06-11"])
    with session_scope() as session:
        report = backtest_service._leakage(good_rows, [late_window], session)
    assert report["passed"] is False and report["window_upto_day"] is False
    assert "形态日晚于评估日" in report["detail"]

    touches_label = dict(good_obs, next_date=good_obs["window"][-1])
    with session_scope() as session:
        report = backtest_service._leakage(good_rows, [touches_label], session)
    assert report["window_excludes_label"] is False and report["passed"] is False

    backwards_label = dict(good_obs, next_date=good_obs["trade_date"])
    with session_scope() as session:
        report = backtest_service._leakage(good_rows, [backwards_label], session)
    assert report["label_after_day"] is False and report["passed"] is False

    dirty_rows = [dict(good_rows[0], sample_window_ok=False)]
    with session_scope() as session:
        report = backtest_service._leakage(dirty_rows, [dict(good_obs)], session)
    assert report["sample_window_before_limit"] is False
    assert "涨停样本窗口不早于涨停日" in report["detail"]


def test_sample_pool_excludes_limit_day_and_dirty_bars(client):
    """样本侧那两条边界：窗口不含涨停当天；error 账里的涨停日不进池。"""
    days = _seed()
    # 06-10 那三天的池子里有 600000@06-08、600001@06-09、600000@06-10 三条，
    # 弄脏最后一条才看得出「只剔这一条」——区间最后一天只有一个涨停样本，弄脏它就是清空。
    pool_day = days[9]
    dirty_day = days[9]
    with session_scope() as session:
        session.add(
            DataQualityError(
                stock_code="600000",
                trade_date=dirty_day,
                check_name="change_percent_check",
                severity=ERROR,
                detail="测试注入",
            )
        )
    with session_scope() as session:
        bars = load_bars_bulk(session, days[-1])
        _dates, samples, _skipped, ok = backtest_service.limit_up_samples_for_backtest(session, pool_day, bars, set())
    assert len(samples) >= 2 and ok is True
    for sample in samples:
        # window 是 ISO 字符串、limit_date 是 date——生产那份 `limit_up_samples` 也是这个形状。
        assert max(sample["window"]) < sample["limit_date"].isoformat()

    with session_scope() as session:
        dirty = error_pairs(session, start=days[0], end=days[-1])
        assert ("600000", dirty_day) in dirty
        _dates, samples, skipped, _ok = backtest_service.limit_up_samples_for_backtest(
            session, pool_day, load_bars_bulk(session, days[-1]), dirty
        )
    assert samples, "剔掉一条脏样本不该把整个池子清空"
    # 同一天还有 600000@06-08 这条干净样本，它必须留下——脏线只脏那一天。
    assert any(
        sample["code"] == "600000" and sample["limit_date"] < dirty_day for sample in samples
    ), samples
    assert dirty_day.isoformat() not in {sample["limit_date"].isoformat() for sample in samples}
    assert any(
        item["code"] == "600000" and item["limit_date"] == dirty_day.isoformat() for item in skipped
    ), skipped
    assert {item["reason"] for item in skipped} & {
        "涨停日当天有 error 级数据质量异常",
        "形态窗口里有 error 级数据质量异常",
    }


# --- 概率列 -------------------------------------------------------------------


def _hand_table(**rates) -> dict:
    return {
        "rates": dict(rates),
        "run_id": 1,
        "min_band_observations": MIN_BAND_OBSERVATIONS,
        "note": "测试用的底表",
    }


def test_probability_without_any_run_is_blank_not_zero(client):
    with session_scope() as session:
        table = backtest_service.probability_table(session)
    assert table["rates"] == {}
    assert table["run_id"] is None
    assert "不是 0%" in table["note"]
    band, percent, samples = backtest_service.probability_for(table, 95.0)
    assert band == "90~100" and percent is None and samples == 0


def test_probability_for_distinguishes_zero_from_unknown():
    # 观测够、发生率真是 0：这是有内容的结论，必须给 0.0 而不是 None。
    hit = _hand_table(**{"90~100": {"percent": 0.0, "samples": 500, "lift": 0.0}})
    assert backtest_service.probability_for(hit, 93.0) == ("90~100", 0.0, 500)
    # 观测不够门槛：留空，但把它是几条观测撑着的说出来。
    thin = _hand_table(**{"90~100": {"percent": 12.5, "samples": MIN_BAND_OBSERVATIONS - 1, "lift": 1.2}})
    assert backtest_service.probability_for(thin, 93.0) == ("90~100", None, MIN_BAND_OBSERVATIONS - 1)
    # 这一档在回测里压根没出现过。
    absent = _hand_table(**{"90~100": {"percent": 8.0, "samples": 300, "lift": 2.0}})
    assert backtest_service.probability_for(absent, 40.0) == ("<60", None, 0)
    assert backtest_service.probability_for(absent, None) == ("", None, 0)


def test_probability_table_reports_which_run_and_which_bands_are_thin(client):
    days = _seed()
    with session_scope() as session:
        payload = backtest_service.run_backtest(session, start=days[0], end=days[-1])
        run_id = backtest_service.persist_run(session, payload)
        table = backtest_service.probability_table(session)
    assert table["run_id"] == run_id
    assert table["day_count"] == payload["day_count"]
    assert f"第 {run_id} 次回测" in table["note"]
    assert str(MIN_BAND_OBSERVATIONS) in table["note"]  # 小样本档点名说噪声
    assert set(table["rates"]) <= set(backtest_service.band_labels())


def test_probability_table_of_a_run_without_buckets_says_so(client):
    """跑过、但一档统计都没落下来：`run_id` 是真的，note 不能让人误以为有依据。"""
    with session_scope() as session:
        session.add(
            BacktestRun(
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5),
                split_ratio=0.5,
                status="done",
                result={"buckets": [], "days": []},
                day_count=3,
                observation_count=0,
                baseline_rate=None,
                note="测试注入：空分档",
                parameters={},
            )
        )
    with session_scope() as session:
        table = backtest_service.probability_table(session)
    assert table["rates"] == {}
    assert table["baseline_percent"] is None
    assert "不是 0%" in table["note"]


def test_band_without_any_result_is_null_not_zero():
    """一条有结果的观测都没有时，发生率是「算不出来」，不是「从没涨停过」。"""
    buckets, baseline = backtest_service._bucket_stats(
        [{"band": "<60", "next_limit_up": None}]
    )
    assert baseline is None
    thin = next(bucket for bucket in buckets if bucket["band"] == "<60")
    assert thin["sample_count"] == 0 and thin["pending_count"] == 1
    assert thin["limit_up_rate"] is None and thin["lift"] is None
    assert all(bucket["limit_up_rate"] is None for bucket in buckets)


def test_realtime_board_row_carries_the_probability_column(client):
    days = _seed()
    with session_scope() as session:
        payload = backtest_service.run_backtest(session, start=days[0], end=days[-1])
        run_id = backtest_service.persist_run(session, payload)
    with session_scope() as session:
        board = realtime_service.scan(session, mode="settled")
    assert board["probability"]["run_id"] == run_id
    assert board["rows"]
    # 小样本：档位观测数不到门槛，整列留空而不是 0%。
    for row in board["rows"]:
        assert "next_day_rate_percent" in row and "similarity_band" in row
        assert row["similarity_band"] == backtest_service.band_of(row["score"])
        assert row["next_day_rate_samples"] >= 0
    assert all(row["next_day_rate_percent"] is None for row in board["rows"])

    # 把底表换成「观测够」的那一份，验证行上的数确实来自这张表，而不是一起留空。
    original = realtime_service.probability_table
    label = board["rows"][0]["similarity_band"]
    realtime_service.probability_table = lambda session: {
        "rates": {label: {"percent": 33.3, "samples": 500, "lift": 1.5}},
        "run_id": run_id,
        "min_band_observations": MIN_BAND_OBSERVATIONS,
        "note": "测试注入的满样本底表",
    }
    try:
        with session_scope() as session:
            swapped = realtime_service.scan(session, mode="settled")
    finally:
        realtime_service.probability_table = original
    top = swapped["rows"][0]
    assert top["similarity_band"] == label
    assert top["next_day_rate_percent"] == 33.3
    assert top["next_day_rate_samples"] == 500
    assert swapped["probability"]["note"] == "测试注入的满样本底表"


# --- 预测池 -------------------------------------------------------------------


def test_carry_evidence_gate_blocks_pool_day_and_future(client):
    """红线：样本涨停日的次日**严格早于**基准日才许查结果。"""
    days = _seed()
    pool_date = days[-1]
    with session_scope() as session:
        # 涨停日正好是基准日前一天：它的「次日」就是基准日，那一天的涨停标记已经被
        # 「当日涨停不进候选」用过一次，不能再来当证据。
        matches = {"000001": [{"code": "600001", "limit_date": days[-2].isoformat(), "score": 92.0}]}
        evidence = prediction_service.carry_evidence(session, matches, pool_date)
    assert evidence["000001"].known == 0
    assert evidence["000001"].missing == 1
    assert evidence["000001"].weighted_rate_percent is None

    with session_scope() as session:
        # 正向对照：同一只票更早那一次涨停（600001 在 days[5] 涨停），次日早于基准日，
        # 这一条必须查得到、并且真的算进连板率。
        early = days[5]
        probe = prediction_service.carry_evidence(
            session,
            {"000002": [{"code": "600001", "limit_date": early.isoformat(), "score": 80.0}]},
            pool_date,
        )
    assert probe["000002"].known == 1, "闸门把已知的一天也挡掉了"
    assert probe["000002"].weighted_rate_percent is not None


def test_build_pool_excludes_today_limit_ups_and_says_no_backtest(client, no_backfill):
    days = _seed()
    with session_scope() as session:
        report = prediction_service.build_pool(session)
    assert no_backfill == []
    assert report["pool_date"] == days[-1].isoformat()
    assert report["calibrated"] is False and report["backtest_run_id"] is None
    assert "还没有任何回测记录" in report["note"]
    assert report["rows"]
    with session_scope() as session:
        limited = {
            code
            for (code,) in session.query(DailyQuote.stock_code)
            .filter(DailyQuote.trade_date == days[-1], DailyQuote.is_limit_up.is_(True))
            .all()
        }
    codes = {row["stock_code"] for row in report["rows"]}
    assert codes & limited == set(), "当日已涨停的进了预测池"
    for row in report["rows"]:
        assert row["band"] in backtest_service.band_labels()
        assert row["band_lift"] is None and row["score_terms"]["band"] is None
        assert 0 <= row["prediction_score"] <= 100
    assert report["next_trade_date"] is None, "区间最后一天之后没有行情，目标日不能编一个出来"
    ordered = [row["prediction_score"] for row in report["rows"]]
    assert ordered == sorted(ordered, reverse=True)
    # 这组夹具里池子那几条涨停的次日都早于基准日，所以连板证据查得到——
    # 恰好证明上面「档位项退场」不是把所有项都退场了。缺项时的权重摊法由
    # `test_composite_score_reweights_missing_terms` 单独盯着。
    assert any(row["carry_rate"] is not None for row in report["rows"])
    for row in report["rows"]:
        if row["carry_rate"] is not None:
            assert 0 <= row["carry_rate"] <= 100
            assert row["score_terms"]["carry"] is not None


def test_build_pool_uses_band_lift_after_a_run(client):
    days = _seed()
    with session_scope() as session:
        payload = backtest_service.run_backtest(session, start=days[0], end=days[-1])
        run_id = backtest_service.persist_run(session, payload)
    label = backtest_service.band_of(payload["observations"][0]["max_similarity"])
    with session_scope() as session:
        report = prediction_service.build_pool(session)
    assert report["backtest_run_id"] == run_id
    assert report["bands"]
    assert f"第 {run_id} 次回测" in report["note"]
    for row in report["rows"]:
        if row["band_samples"] < MIN_BAND_OBSERVATIONS:
            assert row["band_lift"] is None and row["score_terms"]["band"] is None
    # 观测不足门槛是这一轮的必然，页面得能从 note 里读出「为什么不参与打分」。
    assert str(MIN_BAND_OBSERVATIONS) in report["note"] or label == ""


def test_composite_score_reweights_missing_terms():
    score, terms = prediction_service.composite_score(shape=80.0, carry=None, band_lift=None)
    assert score == pytest.approx(80.0) and terms["band"] is None
    score, terms = prediction_service.composite_score(shape=80.0, carry=50.0, band_lift=None)
    assert terms == {"shape": 80.0, "carry": 50.0, "band": None}
    assert score == pytest.approx(round((0.5 * 80 + 0.3 * 50) / 0.8, 2))
    # lift 封顶：3 倍和 2 倍给一样的项分，再高多半是几十条样本抖出来的。
    capped = prediction_service.composite_score(shape=50.0, carry=None, band_lift=3.0)
    at_cap = prediction_service.composite_score(shape=50.0, carry=None, band_lift=2.0)
    assert capped[0] == at_cap[0] and capped[1]["band"] == 100.0


# --- 接口与后台任务 -----------------------------------------------------------


def test_backtest_endpoints_contract(client):
    days = _seed()
    empty = client.get("/api/backtest/latest").json()
    assert empty["run"] is None and empty["probability"]["rates"] == {}

    reversed_range = client.post("/api/backtest/run", params={"start": days[-1].isoformat(), "end": days[0].isoformat()})
    assert reversed_range.status_code == 422 and "晚于结束日" in reversed_range.json()["detail"]
    bad_split = client.post("/api/backtest/run", params={"split_ratio": 1.5})
    assert bad_split.status_code == 422
    empty_db = client.post("/api/backtest/run", params={"start": "2020-01-01", "end": "2020-01-05"})
    assert empty_db.status_code == 404

    started = client.post("/api/backtest/run", params={"max_days": 3})
    assert started.status_code == 200
    task_id = started.json()["task_id"]
    assert task_id and started.json()["days_total"] == 3
    second = client.post("/api/backtest/run")
    assert second.json()["already_running"] is True

    for _ in range(200):
        state = client.get("/api/backtest/status").json()
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["running"] is False, state
    assert state["error"] == "" and state["progress"] == 100
    assert state["task_id"] == task_id
    run_id = state["run_id"]
    assert run_id, f"回测跑完没留账：{state['message']}"

    latest = client.get("/api/backtest/latest").json()
    assert latest["run"]["run_id"] == run_id
    assert latest["run"]["status"] == "done"
    assert latest["probability"]["run_id"] == run_id
    assert client.get("/api/backtest/runs").json()["items"][0]["run_id"] == run_id
    assert client.get(f"/api/backtest/runs/{run_id}").json()["day_count"] == 3
    assert client.get("/api/backtest/runs/9999").status_code == 404

    page = client.get("/api/backtest/observations", params={"run_id": run_id, "limit": 5}).json()
    assert page["total"] == latest["run"]["observation_count"]
    assert len(page["items"]) <= 5
    if page["items"]:
        first = page["items"][0]
        assert first["max_similarity"] >= page["items"][-1]["max_similarity"]
        assert first["trade_date"] in {day.isoformat() for day in days}
    filtered = client.get(
        "/api/backtest/observations", params={"run_id": run_id, "code": "000001"}
    ).json()
    assert all(row["stock_code"] == "000001" for row in filtered["items"])
    assert client.get("/api/backtest/observations", params={"run_id": 9999}).status_code == 404

    pool = client.get("/api/prediction/pool")
    assert pool.status_code == 200
    body = pool.json()
    assert body["pool_date"] == days[-1].isoformat()
    assert body["formula"]

    assert client.delete(f"/api/backtest/runs/{run_id}").json()["ok"] is True
    assert client.get(f"/api/backtest/runs/{run_id}").status_code == 404
    assert client.delete("/api/backtest/runs/9999").status_code == 404
    assert client.get("/api/backtest/latest").json()["run"] is None


def test_backtest_start_rejects_when_library_is_empty(client):
    state = client.get("/api/backtest/status").json()
    assert state["running"] is False and state["run_id"] is None
    started = client.post("/api/backtest/run")
    assert started.status_code == 404
    assert "没有任何行情" in started.json()["detail"]


def test_failed_backtest_leaves_a_row_but_no_probability(client, monkeypatch, no_backfill):
    """跑砸了要在台账里说破：那一行是 failed、有原因；概率列仍旧是空的。

    两重对照缺一不可：`run_id` 在失败后必须还指得到台账里那一条；同一条读取路径
    在真跑成功时必须读得出 done 与那一轮——否则「读不到」是查询坏了，不是状态生效。
    """
    _seed()

    def boom(*_args, **_kwargs):
        raise RuntimeError("打分中途炸了")

    monkeypatch.setattr(backtest_service, "run_backtest", boom)
    started = client.post("/api/backtest/run", params={"max_days": 2})
    assert started.status_code == 200
    run_id = started.json()["run_id"]
    assert run_id, "开跑前就该有占位行，它的 run_id 要当场交回页面"

    for _ in range(200):
        state = client.get("/api/backtest/status").json()
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["running"] is False
    assert "打分中途炸了" in state["error"]
    assert state["run_id"] == run_id, "失败后把 run_id 丢了，页面指不回台账里那一条"

    runs = client.get("/api/backtest/runs").json()["items"]
    failed = next(item for item in runs if item["run_id"] == run_id)
    assert failed["status"] == "failed"
    assert "打分中途炸了" in failed["error"]
    assert failed["note"].startswith("跑砸了")
    assert failed["finished_at"]
    assert client.get(f"/api/backtest/runs/{run_id}").json()["status"] == "failed"

    latest = client.get("/api/backtest/latest").json()
    assert latest["run"] is None, "失败的那一轮被下游当成结果读到了"
    assert latest["probability"]["rates"] == {}
    assert latest["probability"]["run_id"] is None

    # 正向对照：换回真实现，同一套读取路径必须读得出 done 与这一轮。
    monkeypatch.undo()
    restarted = client.post("/api/backtest/run", params={"max_days": 3})
    assert restarted.status_code == 200
    for _ in range(200):
        state = client.get("/api/backtest/status").json()
        if not state["running"]:
            break
        time.sleep(0.05)
    assert state["error"] == ""
    latest = client.get("/api/backtest/latest").json()
    assert latest["run"]["run_id"] == state["run_id"]
    assert latest["run"]["status"] == "done"
    assert state["run_id"] != run_id


def test_stale_running_rows_are_retired_without_touching_done_rows(client):
    """进程死在回测里，那一行 status 会停在 running；启动时记成 failed，done 行一个字不动。"""
    assert client.get("/api/backtest/status").json()["run_id"] is None
    with session_scope() as session:
        stale = BacktestRun(
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 3),
            split_ratio=0.5,
            status="running",
            result={},
            day_count=0,
            observation_count=0,
            baseline_rate=None,
            note="正在回测",
            error="",
            parameters={},
        )
        done = BacktestRun(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 3),
            split_ratio=0.5,
            status="done",
            result={},
            day_count=3,
            observation_count=10,
            baseline_rate=0.1,
            note="跑完了",
            error="",
            parameters={},
            finished_at=datetime(2026, 5, 3, 15, 0),
        )
        session.add_all([stale, done])
        session.flush()
        stale_id, done_id = int(stale.id), int(done.id)

    with session_scope() as session:
        assert backtest_service.retire_stale_runs(session) == 1
        assert backtest_service.retire_stale_runs(session) == 0

    with session_scope() as session:
        retired = session.get(BacktestRun, stale_id)
        assert retired.status == "failed"
        assert "进程重启" in retired.error and "进程重启" in retired.note
        assert retired.finished_at is not None
        untouched = session.get(BacktestRun, done_id)
        assert untouched.status == "done" and untouched.error == ""
        assert untouched.note == "跑完了" and untouched.finished_at == datetime(2026, 5, 3, 15, 0)
        assert int(backtest_service.latest_done_run(session).id) == done_id


def test_ai_evidence_reads_the_band_rate_from_the_board(client):
    """那条砍掉过的证据接回来了：概率列进账，出处写在 `history_source` 里。"""
    days = _seed()
    with session_scope() as session:
        payload = backtest_service.run_backtest(session, start=days[0], end=days[-1])
        run_id = backtest_service.persist_run(session, payload)
    with session_scope() as session:
        board = realtime_service.scan(session, mode="settled")
        assert board["probability"]["run_id"] == run_id
    # 给模型看的那份榜是 `realtime_tracker._board` 折出来的两榜——直接喂 scan 的 `rows`
    # 它会一条都读不到（它读 `limit_up_rows` / `pending_rows`）。
    feeding = realtime_tracker._board(board, top_n=5, metric="score")
    with session_scope() as session:
        evidence = ai_evidence.build_evidence(session, feeding)
    assert evidence["probability_run_id"] == run_id
    assert f"第 {run_id} 次回测" in evidence["probability_note"]
    assert evidence["rows"], "榜上有行却没有一行证据，这一项判据成了空话"
    for row in evidence["rows"]:
        assert "band_next_day_rate_percent" in row
        assert row["formula"]["history_source"] in {None, "band_rate", "ai_pick_ledger"}
    # 小样本：没有一档过观测门槛，档位发生率一项全留空，模型不能拿到一个编出来的历史成功率。
    assert evidence["band_rate_rows"] == 0
    assert all(row["band_next_day_rate_percent"] is None for row in evidence["rows"])

    # 正向对照：把底表换成满样本那一份，行上的档位发生率必须进证据、
    # 并且把「历史成功率这一项吃的是档位发生率」写进 history_source。
    label = evidence["rows"][0]["similarity_band"]
    assert label, "榜上那一行连档位都没有，注入对照无从做起"
    original = realtime_service.probability_table
    realtime_service.probability_table = lambda session: {
        "rates": {label: {"percent": 41.5, "samples": 600, "lift": 1.8}},
        "run_id": run_id,
        "min_band_observations": MIN_BAND_OBSERVATIONS,
        "note": "测试注入的满样本底表",
    }
    try:
        with session_scope() as session:
            injected = realtime_service.scan(session, mode="settled")
        feeding = realtime_tracker._board(injected, top_n=5, metric="score")
        with session_scope() as session:
            full = ai_evidence.build_evidence(session, feeding)
    finally:
        realtime_service.probability_table = original
    matched = [row for row in full["rows"] if row["similarity_band"] == label]
    assert matched, "注入的那一档在证据行里找不到对应股票"
    for row in matched:
        assert row["band_next_day_rate_percent"] == 41.5
        assert row["band_next_day_samples"] == 600
        assert row["formula"]["history_source"] == "band_rate"
        assert row["indicator_scores"]["history_limit_up_rate"] == pytest.approx(41.5)
    assert full["band_rate_rows"] == len(matched)
    assert "档内次日 41.5%" in ai_evidence.evidence_line(matched[0])

