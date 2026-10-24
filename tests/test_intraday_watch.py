"""盘中涨幅报警榜：池子从哪来、两档怎么算、报过几次、置顶怎么解除。

四条判据各配一次注入式违规，因为它们是四种不同的坏法：

* `None` 当成 0% → 停牌股每天开盘就被报一次「涨过 3%」；
* 采样不足当成「在冲」 → 拿一个窗口外的旧价当 3 分钟前的参照，凭空造出 6 个点的跳升；
* 去重只写在 Python 里 → 服务重启、两个进程各报一条，同一只一天响两遍；
* 数据源回的是昨天的价也报警 → 报出来的是真话配错了日期。

真网关一次都不敲：夹具把 `call_model` 换成一个会当场报错的假货，
报警榜这条链路要是哪天悄悄去要结论，测试会先炸。
"""

import time
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from backend.app.core import clock
from backend.app.core.constants import (
    ALERT_PIN_PERCENT,
    ALERT_POPUP_PERCENT,
    ALERT_RETENTION_DAYS,
    ALERT_SPIKE_POINTS,
    ALERT_SPIKE_WINDOW_SECONDS,
)
from backend.app.core.database import session_scope
from backend.app.models.tables import AiDailyRankBoard, IntradayWatchAlert, TradingDay
from backend.app.services import ai_gateway
from backend.app.services.ai_daily_rank_service import persist_outcome, upsert_prepared
from backend.app.services.intraday_watch import (
    POOL_ABSENT,
    POOL_BROKEN,
    POOL_FAILED,
    POOL_NO_DAY,
    POOL_PREPARED,
    POOL_READY,
    STATE_AFTER_CLOSE,
    STATE_PRE_OPEN,
    CodeSeries,
    WatchPool,
    build_pool,
    hit_alert,
    hit_spike,
    previous_trade_date,
    watcher,
)
from backend.app.services.intraday_watch_store import (
    TIER_ALERT,
    TIER_SPIKE,
    ack_alerts,
    alerts_of,
    pending_pin_codes,
    prune,
    record_alert,
)
from backend.app.services.realtime_service import set_spot_fetcher

#: 报警榜盯的是「今天」，所以种子日必须落在被钉住的那个 `clock.today()` 上；
#: 「前一交易日」往前退到工作日，周末跑测试时池子也有一句说得出口的昨天。
TODAY = date.today()


def _last_weekday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


PREV = _last_weekday(TODAY)
_OLD = "%Y-%m-%dT%H:%M:%S"


# --- 造数据 ------------------------------------------------------------------


def _picks(codes: list[str]) -> list[dict]:
    #: 名次故意倒着给：池子必须按模型给的 `rank` 重排，而不是照数组顺序抄一遍
    return [
        {
            "stock_code": code,
            "stock_name": f"测试{code}",
            "score": 90.0 - index,
            "rank": len(codes) - index,
            "reason": "封板整齐",
            "counter_evidence": "样本只有几天",
            "feed_rank": index + 1,
        }
        for index, code in enumerate(codes)
    ]


def _ranked_board(day: date, codes: list[str], *, with_scores: bool = True) -> None:
    """做出「那一天已经排好名次」的那一行：走真实的备输入 + 落库，不手拼 payload。"""
    picks = _picks(codes)
    if not with_scores:
        picks = [{"stock_code": item["stock_code"], "stock_name": item["stock_name"]} for item in picks]
    with session_scope() as session:
        upsert_prepared(
            session,
            trade_date=day.isoformat(),
            base_date=_last_weekday(day).isoformat(),
            window_mode="settled",
            metric="score",
            metric_label="综合分",
            model="test-model",
            board_rows=len(codes),
            feed_rows=len(codes),
            feed=[
                {
                    "feed_rank": index + 1,
                    "stock_code": code,
                    "stock_name": f"测试{code}",
                }
                for index, code in enumerate(codes)
            ],
            extra={},
        )
        persist_outcome(
            session,
            {
                "ok": True,
                "trade_date": day.isoformat(),
                "model": "test-model",
                "started_at": "",
                "finished_at": "",
                "latency_seconds": None,
                "result": {
                    "picks": picks,
                    "picks_count": len(picks),
                    "criteria": "测试用口径",
                },
            },
        )


def _prepared_board(day: date, codes: list[str]) -> None:
    with session_scope() as session:
        upsert_prepared(
            session,
            trade_date=day.isoformat(),
            base_date=_last_weekday(day).isoformat(),
            window_mode="settled",
            metric="score",
            metric_label="综合分",
            model="test-model",
            board_rows=len(codes),
            feed_rows=len(codes),
            feed=[{"feed_rank": index + 1, "stock_code": code} for index, code in enumerate(codes)],
            extra={},
        )


def _board_with_status(day: date, status: str, *, payload: dict | None = None, error: str = "") -> None:
    with session_scope() as session:
        session.add(AiDailyRankBoard(trade_date=day, status=status, error=error, payload=payload or {}))


def _clear_boards() -> None:
    with session_scope() as session:
        session.query(AiDailyRankBoard).delete()


def _calendar(*days: date) -> None:
    with session_scope() as session:
        for day in days:
            if session.get(TradingDay, day) is None:
                session.add(TradingDay(trade_date=day, is_open=True, limit_up_count=0))


# --- 夹具 -------------------------------------------------------------------


@pytest.fixture()
def watch_env(client, monkeypatch):
    """钉住时钟、注入快照源、把网关换成会咬手的假货。

    交易时段也必须钉：`run_round` 里那一句「盘前盘后不敲上游」读的是墙钟，
    不钉住的话同一份测试在早上跑和晚上跑结果是两样。
    """
    from backend.app.api import routes

    monkeypatch.setattr(clock, "today", lambda: TODAY)
    monkeypatch.setattr(routes, "today", lambda: TODAY)
    wall = {"moment": _at(TODAY, 10, 0)}
    monkeypatch.setattr(clock, "now", lambda: wall["moment"])

    def paid(*_args, **_kwargs):
        raise AssertionError("报警榜这条路花钱了：一次网关都不该敲")

    monkeypatch.setattr(ai_gateway, "call_model", paid)

    holder = {"rows": [], "calls": 0}

    def fetcher(_name, codes):
        holder["calls"] += 1
        wanted = set(codes)
        return [row for row in holder["rows"] if row["code"] in wanted]

    set_spot_fetcher(fetcher)
    watcher.reset()
    yield {"client": client, "holder": holder, "wall": wall}

    #: 收尾要等到线程真死：它在下一拍就会拿「没被 patch 的时钟 + 真行情源」再跑一轮，
    #: 那一轮的账属于这台机器上的真实行情，不属于这个测试。
    watcher.stop()
    thread = watcher._thread
    if thread is not None:
        until = time.monotonic() + 10.0
        while thread.is_alive() and time.monotonic() < until:
            time.sleep(0.05)
        if thread.is_alive():
            pytest.fail("报警榜监控线程没在 10 秒里退出")
    watcher.reset()
    set_spot_fetcher()


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, datetime.min.time().replace(hour=hour, minute=minute))


def _quote(code: str, percent: float | None, *, day: date = TODAY, price: float = 10.0) -> dict:
    return {
        "code": code,
        "close": price,
        "pre_close": round(price / (1.0 + (percent or 0.0) / 100.0), 2),
        "pct_chg": percent,
        "trade_date": day,
        "source": "mock",
    }


def _seed_window(code: str, past_percent: float, *, seconds_ago: float = 200.0) -> None:
    """往内存序列里补一个「窗口之外」的参考点。**必须在池子已经定下来之后调用**。

    不这么写就只能真等 3 分钟才测得到第二档。这一份序列本来就只是内存里的采样缓冲，
    落库的是报警而不是价格，所以直接喂是安全的，也不是伪造行情。

    必须等第一轮之后再喂：换交易日时 `_pool_for` 会把整个序列丢掉（3 分钟窗口不许跨天拼），
    在第一天第一轮之前喂进去的参考点会当场被清掉。
    """
    series = watcher._series.setdefault(code, CodeSeries())
    series.samples.clear()
    series.add(at=time.monotonic() - seconds_ago, percent=past_percent, last_price=9.0)


# --- 判据（纯函数）-----------------------------------------------------------


def test_no_price_is_not_zero_percent():
    """`None` 不许当 0%，也不许当「已经涨过了」：停牌那天它一个点都没涨。"""
    assert hit_alert(None) is False
    assert hit_alert(ALERT_POPUP_PERCENT - 0.01) is False
    assert hit_alert(ALERT_POPUP_PERCENT) is True


def test_spike_needs_the_level_and_the_rise():
    """第二档是「并且」：只看 6% 会把昨天涨完、今天横着开盘的那只一进门就摞到最前。"""
    assert hit_spike(ALERT_PIN_PERCENT, None) is False  # 采样不足是「不知道」，不是「在冲」
    assert hit_spike(None, 9.9) is False
    assert hit_spike(ALERT_PIN_PERCENT, ALERT_SPIKE_POINTS - 0.01) is False
    assert hit_spike(ALERT_PIN_PERCENT - 0.01, 9.0) is False
    assert hit_spike(ALERT_PIN_PERCENT, ALERT_SPIKE_POINTS) is True


def test_rise_uses_the_nearest_sample_at_the_window_edge():
    """「3 分钟前」取的是不晚于那一刻的最后一个样本，不是缓冲里最老那个。

    负向对照就写在数字里：最老那个是 1.0%，边上那个是 3.0%；拿错了就把 2.0 个点
    的跳升报成 4.0——第二档被凭空放大一倍。
    """
    series = CodeSeries()
    now = time.monotonic()
    for stamp, percent in ((now - 260, 1.0), (now - 220, 3.0), (now - 30, 4.0)):
        series.samples.append((stamp, percent))
    series.percent = 5.0
    assert series.rise_in_window(at=now) == pytest.approx(2.0)
    #: 窗口里一个参考点都没有时只能说「不知道」
    series.percent = 5.0
    series.samples.clear()
    series.samples.append((now - 20, 1.0))
    assert series.rise_in_window(at=now) is None


def test_a_snapshot_gap_does_not_borrow_a_stale_reference():
    """取不到价那一轮不把 `None` 写进序列，也不许留着窗口外的旧点冒充「3 分钟前」。"""
    series = CodeSeries()
    now = time.monotonic()
    series.add(at=now - 300, percent=2.0, last_price=9.0)
    series.add(at=now - 100, percent=None, last_price=None)  # 这一轮源没回价
    assert [stamp for stamp, _value in series.samples] == [pytest.approx(now - 300)]
    assert all(value is not None for _stamp, value in series.samples)
    series.add(at=now, percent=8.0, last_price=11.0)
    #: 那个 300 秒前的点已经被掐掉：剩下的只有这一轮，窗口另一头不存在
    assert len(series.samples) == 1
    assert series.rise_in_window(at=now) is None
    assert series.percent == 8.0 and series.last_price == 11.0
    assert series.span_seconds(at=now) == pytest.approx(0.0)
    #: 反向对照：如果掐头没生效，这里会报出一个 6.0 个点的假跳升，第二档当场就响
    stale = CodeSeries()
    stale.samples.append((now - 300, 2.0))
    stale.percent = 8.0
    assert stale.rise_in_window(at=now) == pytest.approx(6.0)


# --- 池子 -------------------------------------------------------------------


def test_pool_walks_the_four_ways_a_board_can_be_missing(client):
    """四种缺池各回各的话：都写成「暂无数据」，用户就永远不知道该去点哪一下。"""
    _calendar(PREV, TODAY)
    cases = [
        (POOL_ABSENT, lambda: None, "没有可盯"),
        (POOL_PREPARED, lambda: _prepared_board(PREV, ["600000"]), "没有排名"),
        (POOL_FAILED, lambda: _board_with_status(PREV, "failed", error="HTTP 429 被挡回"), "没有成功"),
        (POOL_BROKEN, lambda: _board_with_status(PREV, "done", payload={}), "读不动"),
    ]
    for status, seed, phrase in cases:
        _clear_boards()
        if seed is not None:
            seed()
        with session_scope() as session:
            pool = build_pool(session, TODAY)
        assert pool.status == status, (status, pool.status, pool.note)
        assert pool.rows == [] and pool.ready is False
        assert phrase in pool.note, pool.note
        if status == POOL_FAILED:
            assert "HTTP 429" in pool.note  # 那一次的错要能带给用户，不能只说「没成功」


def test_pool_needs_an_earlier_trading_day_at_all(client):
    with session_scope() as session:
        session.query(TradingDay).delete()
        session.query(AiDailyRankBoard).delete()
        pool = build_pool(session, TODAY)
    assert pool.status == POOL_NO_DAY and pool.pool_date is None
    assert "早于" in pool.note and "同步" in pool.note, pool.note


def test_pool_is_yesterday_ranked_board_and_keeps_the_ranking_owner(client):
    codes = ["600000", "000002", "600001"]
    _calendar(PREV, TODAY)
    _ranked_board(PREV, codes)
    with session_scope() as session:
        pool = build_pool(session, TODAY)
    assert pool.status == POOL_READY and pool.ready is True
    assert pool.trade_date == TODAY.isoformat() and pool.pool_date == PREV.isoformat()
    #: 名次归模型：给的是倒序，出来必须按 `rank` 正过来
    assert [row["stock_code"] for row in pool.rows] == list(reversed(codes))
    assert [row["rank"] for row in pool.rows] == [1, 2, 3]
    assert pool.rows[0]["stock_name"] == "测试600001"
    assert "AI 固定榜" in pool.note and "不改它的顺序" in pool.note
    #: 缺分缺名次的那几行不补 0：读不出来就是 None，页面显示「—」
    _clear_boards()
    _ranked_board(PREV, ["600000"], with_scores=False)
    with session_scope() as session:
        row = build_pool(session, TODAY).rows[0]
    assert row["rank"] is None and row["score"] is None


def test_previous_trade_date_ignores_today_and_the_future(client):
    _calendar(PREV - timedelta(days=1), PREV, TODAY, TODAY + timedelta(days=3))
    with session_scope() as session:
        assert previous_trade_date(session, TODAY) == PREV


# --- 账本 -------------------------------------------------------------------


def test_one_alert_per_stock_per_day_per_tier(client):
    with session_scope() as session:
        first = record_alert(
            session, trade_date=TODAY.isoformat(), stock_code="600000", stock_name="浦发银行",
            tier=TIER_ALERT, percent=3.4, rank=2, score=88.0,
        )
        again = record_alert(
            session, trade_date=TODAY.isoformat(), stock_code="600000", stock_name="浦发银行",
            tier=TIER_ALERT, percent=7.8, rank=2, score=88.0,
        )
    assert first is not None and again is None
    #: 第二次不许覆盖第一次的账：那条报警追不出「几点、百分之几报的」就没了意义
    assert first["percent"] == pytest.approx(3.4) and first["tier_label"]
    with session_scope() as session:
        assert len(alerts_of(session, TODAY.isoformat())) == 1
    #: 先查后插只是省一次异常，真正的判据是库：绕过那句查询也插不进第二行
    with session_scope() as session:
        session.add(
            IntradayWatchAlert(
                trade_date=TODAY, stock_code="600000", tier=TIER_ALERT, percent=9.9,
                fired_at="2026-01-01T09:35:00", acked_at="",
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        #: 撞了唯一约束就把这条 session 退干净：`session_scope` 收尾要 commit，
        #: 留着待回滚的事务会让这一句变成「PendingRollback」而不是「这条判据生效了」
        session.rollback()
    #: 另一档是另一条账：同一只涨到 6% 以上还要报第二档
    with session_scope() as session:
        spike = record_alert(
            session, trade_date=TODAY.isoformat(), stock_code="600000", stock_name="浦发银行",
            tier=TIER_SPIKE, percent=6.5, rise_3min=2.6,
        )
    assert spike is not None and spike["rise_3min"] == pytest.approx(2.6)
    #: 坏输入不编账：日期读不出、档位不认识，一律不写
    with session_scope() as session:
        assert record_alert(
            session, trade_date="不是日子", stock_code="600001", stock_name="", tier=TIER_ALERT, percent=9.0
        ) is None
        assert record_alert(
            session, trade_date=TODAY.isoformat(), stock_code="600002", stock_name="", tier="tier_99", percent=9.0
        ) is None


def test_ack_is_scoped_to_today_and_keeps_the_first_ack_time(client):
    yesterday = TODAY - timedelta(days=1)
    with session_scope() as session:
        record_alert(
            session, trade_date=TODAY.isoformat(), stock_code="600000", stock_name="甲",
            tier=TIER_SPIKE, percent=6.8, rise_3min=3.1,
        )
        record_alert(
            session, trade_date=TODAY.isoformat(), stock_code="000002", stock_name="乙",
            tier=TIER_SPIKE, percent=7.4, rise_3min=2.2,
        )
        record_alert(
            session, trade_date=yesterday.isoformat(), stock_code="600000", stock_name="甲",
            tier=TIER_SPIKE, percent=6.1, rise_3min=2.0,
        )
        assert pending_pin_codes(session, TODAY.isoformat()) == ["000002", "600000"]
        assert ack_alerts(session, TODAY.isoformat(), stock_code="600000", tier=TIER_SPIKE) == 1
        ledger = alerts_of(session, TODAY.isoformat())
        acked = [item for item in ledger if item["stock_code"] == "600000"][0]
        assert acked["acked_at"] and pending_pin_codes(session, TODAY.isoformat()) == ["000002"]
        #: 重复点不许把确认时刻刷成新值：页面上「几点取消的置顶」要保得住
        kept = acked["acked_at"]
        assert ack_alerts(session, TODAY.isoformat(), stock_code="600000") == 0
        again = [item for item in alerts_of(session, TODAY.isoformat()) if item["stock_code"] == "600000"][0]
        assert again["acked_at"] == kept
        #: 昨天那几条没点的不会被这一句顺手清掉，也不会再来敲今天这一次
        assert pending_pin_codes(session, yesterday.isoformat()) == ["600000"]
        assert ack_alerts(session, TODAY.isoformat()) == 1
        assert pending_pin_codes(session, TODAY.isoformat()) == []


def test_first_tier_never_pins_the_window(client):
    """置顶只认第二档：「涨过 3%」是提示，用户拍板的是「直线上涨」。"""
    with session_scope() as session:
        record_alert(
            session, trade_date=TODAY.isoformat(), stock_code="600000", stock_name="甲",
            tier=TIER_ALERT, percent=3.2,
        )
        assert pending_pin_codes(session, TODAY.isoformat()) == []
        assert len(alerts_of(session, TODAY.isoformat())) == 1


def test_prune_cuts_both_ledger_shapes(client):
    old = (datetime.now() - timedelta(days=ALERT_RETENTION_DAYS + 1)).strftime(_OLD)
    fresh = (datetime.now() - timedelta(days=ALERT_RETENTION_DAYS - 1)).strftime(_OLD)
    with session_scope() as session:
        session.query(IntradayWatchAlert).delete()
        #: 三种行各归一种：窗口外的老账按 `fired_at` 裁；补列之前那批（`fired_at` 空）按
        #: `created_at` 裁；窗口内的正常账一条都不许动。
        session.add(
            IntradayWatchAlert(trade_date=TODAY - timedelta(days=40), stock_code="600001",
                               tier=TIER_ALERT, percent=3.0, fired_at=old, acked_at="")
        )
        legacy = IntradayWatchAlert(trade_date=TODAY - timedelta(days=40), stock_code="600002",
                                    tier=TIER_ALERT, percent=3.0, fired_at="", acked_at="")
        legacy.created_at = datetime.now() - timedelta(days=ALERT_RETENTION_DAYS + 2)
        session.add(legacy)
        session.add(
            IntradayWatchAlert(trade_date=TODAY, stock_code="600003", tier=TIER_ALERT,
                               percent=3.0, fired_at=fresh, acked_at="")
        )
        session.flush()
        removed = prune(session)
        left = {row.stock_code for row in session.query(IntradayWatchAlert).all()}
    assert removed == 2 and left == {"600003"}


# --- 一轮采样 ---------------------------------------------------------------


def test_a_round_alerts_then_pins_and_never_twice(watch_env):
    holder = watch_env["holder"]
    _calendar(PREV, TODAY)
    _ranked_board(PREV, ["600000", "000002"])

    #: 第一拍：600000 涨过 3%、还没到 6%；000002 只有 1%
    holder["rows"] = [_quote("600000", 3.4), _quote("000002", 1.0)]
    state = watcher.run_round()
    assert state["status"] == "watching", state
    assert state["sampling"]["snapshot_date"] == TODAY.isoformat()
    assert [item["tier"] for item in state["alerts"]] == [TIER_ALERT]
    assert state["pinning"] is False and state["pending_pin_codes"] == []
    row = {item["stock_code"]: item for item in state["rows"]}["600000"]
    assert row["alert"] is not None and row["spike"] is None
    assert row["change_percent"] == pytest.approx(3.4)
    #: 没有 3 分钟前的参考点时这一列是空的，不是 0
    assert row["rise_3min"] is None and row["sample_span_seconds"] == pytest.approx(0.0)
    assert {item["stock_code"]: item["no_snapshot"] for item in state["rows"]}["000002"] is False

    #: 第二拍：补一个窗口外的参考点，600000 冲到 6.5%
    _seed_window("600000", 3.9)
    holder["rows"] = [_quote("600000", 6.5), _quote("000002", 1.0)]
    state = watcher.run_round()
    assert state["pinning"] is True and state["pending_pin_codes"] == ["600000"]
    assert {item["tier"] for item in state["alerts"]} == {TIER_ALERT, TIER_SPIKE}

    #: 第三拍：同一只、同一档，库里已经有行——一个字都不许多报
    calls = holder["calls"]
    state = watcher.run_round()
    assert len(state["alerts"]) == 2 and holder["calls"] == calls + 1
    #: `rounds` 报的是「这一轮之前已经跑了几轮」：第三轮那份状态里是 2
    assert state["sampling"]["rounds"] == 2


def test_ack_ends_the_pin_without_resampling(watch_env):
    holder = watch_env["holder"]
    _calendar(PREV, TODAY)
    _ranked_board(PREV, ["600000"])
    #: 先跑一拍把池子定下来：换交易日时采样序列会被丢掉，参考点要在那之后喂
    holder["rows"] = [_quote("600000", 1.0)]
    watcher.run_round()
    _seed_window("600000", 4.5)
    holder["rows"] = [_quote("600000", 7.2)]
    assert watcher.run_round()["pinning"] is True

    #: 「知道了」不许再敲一次上游：那是用户按下按钮那一刻必须生效的事
    calls = holder["calls"]
    result = watcher.ack(TODAY.isoformat(), tier=TIER_SPIKE)
    assert result["ok"] is True and result["acked"] == 1
    assert result["pinning"] is False and result["pending_pin_codes"] == []
    assert result["remaining"] == 1  # 第一档那条还挂着，但窗口不置顶了
    assert holder["calls"] == calls
    #: 下一次读状态必须当场看见账变了（不许还答「没点过」）
    state = watcher.state()
    assert state["pinning"] is False and len(state["alerts"]) == 2
    assert watcher.pin_codes() == []
    spike = [item for item in state["alerts"] if item["tier"] == TIER_SPIKE][0]
    assert spike["acked_at"], spike


def test_a_stale_snapshot_is_not_sampled_and_not_alerted(watch_env):
    """数据源回的不是今天的价：这一轮不采样、不报警，只把这句话带给页面。"""
    holder = watch_env["holder"]
    _calendar(PREV, TODAY)
    _ranked_board(PREV, ["600000"])
    holder["rows"] = [_quote("600000", 9.0, day=PREV)]
    state = watcher.run_round()
    assert state["status"] == "unavailable", state
    assert state["sampling"]["snapshot_date"] == PREV.isoformat()
    assert state["alerts"] == [] and state["pinning"] is False
    assert state["rows"][0]["change_percent"] is None, "上一天的价被写进了这一轮的榜"
    assert watcher._series == {} or all(
        series.percent is None for series in watcher._series.values()
    ), "没有今天的价却把昨天那个价留成了这一轮的读数"
    assert PREV.isoformat() in state["note"]
    with session_scope() as session:
        assert alerts_of(session, TODAY.isoformat()) == []


def test_out_of_session_rounds_never_touch_the_quotes(watch_env):
    """盘前与盘后都不敲上游：那两段拿到的价是昨天收盘，报在今天就是配错日期的真话。"""
    holder = watch_env["holder"]
    _calendar(PREV, TODAY)
    _ranked_board(PREV, ["600000"])
    holder["rows"] = [_quote("600000", 8.0)]
    for moment, expected in ((_at(TODAY, 9, 0), STATE_PRE_OPEN), (_at(TODAY, 15, 30), STATE_AFTER_CLOSE)):
        watch_env["wall"]["moment"] = moment
        calls = holder["calls"]
        state = watcher.run_round()
        assert state["status"] == expected and state["session_phase"] == expected, state
        assert holder["calls"] == calls, "盘前/盘后还是去敲了行情源"
        assert "采样" in state["note"]
        assert state["alerts"] == []
    #: 时段恢复后同一档就正常采样（负向对照的另一半：不是整档都不干活）
    watch_env["wall"]["moment"] = _at(TODAY, 10, 0)
    state = watcher.run_round()
    assert state["status"] == "watching" and holder["calls"] > 0
    #: 刚才那两拍没采到价，所以这一拍只有手里这一个点：报第一档，报不出第二档
    assert [item["tier"] for item in state["alerts"]] == [TIER_ALERT]


def test_a_broken_round_leaves_a_readable_state(watch_env, monkeypatch):
    """一轮里任何一步炸了都只变成状态里的一句 `error`，不许把线程带崩、也不许留空榜。"""
    _calendar(PREV, TODAY)
    _ranked_board(PREV, ["600000"])
    from backend.app.services import intraday_watch

    def explode(_codes):
        raise ZeroDivisionError("除零：假装取数这一步崩了")

    monkeypatch.setattr(intraday_watch, "fetch_spot", explode)
    state = watcher.run_round()
    assert state["status"] == "unavailable"
    assert "除零" in state["sampling"]["error"] and "取盘中快照失败" in state["note"]
    assert state["row_count"] == 1 and state["rows"][0]["no_snapshot"] is True


def test_no_source_answers_is_said_in_words(watch_env):
    """一个价都没回时不许显示成「今天没有一只在涨」。"""
    holder = watch_env["holder"]
    _calendar(PREV, TODAY)
    _ranked_board(PREV, ["600000"])
    holder["rows"] = []
    state = watcher.run_round()
    assert state["status"] == "unavailable"
    assert "空的盘中快照" in state["note"], state["note"]


def test_an_empty_pool_says_why_and_watches_nothing(watch_env):
    """库里没有前一天的榜时：这一档是空池 + 一句原因，绝不拿更早的榜充数。"""
    _calendar(PREV, TODAY)
    _clear_boards()
    watch_env["holder"]["rows"] = [_quote("600000", 9.9)]
    calls = watch_env["holder"]["calls"]
    state = watcher.run_round()
    assert state["status"] == "no_pool" and state["row_count"] == 0
    assert state["pool"]["status"] == POOL_ABSENT
    assert watch_env["holder"]["calls"] == calls
    with session_scope() as session:
        assert alerts_of(session, TODAY.isoformat()) == []


def test_the_pool_is_cached_for_the_whole_day(watch_env):
    """盘中不换成员：池子按交易日缓存，别因为「昨天那份榜被重跑」就换人。"""
    _calendar(PREV - timedelta(days=1), PREV, TODAY)
    _ranked_board(PREV, ["600000"])
    watch_env["holder"]["rows"] = [_quote("600000", 1.0)]
    watcher.run_round()
    first = watcher._pool
    _ranked_board(PREV - timedelta(days=1), ["000002"])
    watcher.run_round()
    assert watcher._pool is first
    assert [row["stock_code"] for row in watcher._pool.rows] == ["600000"]


# --- 接口 -------------------------------------------------------------------


def test_watch_api_reads_and_acks(watch_env):
    _calendar(PREV, TODAY)
    _ranked_board(PREV, ["600000"])
    client = watch_env["client"]
    watch_env["holder"]["rows"] = [_quote("600000", 1.0)]
    watcher.run_round()  # 先把池子定下来，参考点要在那之后喂
    _seed_window("600000", 4.4)
    watch_env["holder"]["rows"] = [_quote("600000", 7.0)]
    watcher.run_round()

    payload = client.get("/api/realtime/intraday-watch").json()
    #: 阈值只能从后端来：前端各写一份常量迟早和判据漂开
    assert payload["alert_percent"] == ALERT_POPUP_PERCENT
    assert payload["pin_percent"] == ALERT_PIN_PERCENT
    #: 档位标识也从后端来：页面不许自己抄 `'spike_6pct'` 这种字面量
    assert payload["alert_tier"] == TIER_ALERT and payload["pin_tier"] == TIER_SPIKE
    assert payload["rise_window_seconds"] == ALERT_SPIKE_WINDOW_SECONDS
    assert payload["rise_min_points"] == ALERT_SPIKE_POINTS
    assert payload["pool_size"] == 20 and payload["pool"]["row_count"] == 1
    assert {
        "status", "trade_date", "session_phase", "pool", "rows", "alerts", "unacked",
        "pending_pin_codes", "pinning", "sampling", "poll_seconds", "disclaimer", "note",
    } <= set(payload)
    assert payload["pinning"] is True and len(payload["unacked"]) == 2
    assert payload["sampling"]["thread_running"] is True, "报警榜的轮次必须住在后台线程里"

    bad = client.post("/api/realtime/intraday-watch/ack", params={"tier": "tier_99"})
    assert bad.status_code == 422 and "档位" in bad.json()["detail"]

    acked = client.post("/api/realtime/intraday-watch/ack", params={"stock_code": "600000"}).json()
    assert acked["ok"] is True and acked["trade_date"] == TODAY.isoformat()
    assert acked["acked"] == 2 and acked["remaining"] == 0
    assert acked["pinning"] is False and acked["pending_pin_codes"] == []
    #: 不用重进页面：紧接着这一次读就该是刚确认完的那个事实
    assert client.get("/api/realtime/intraday-watch").json()["pinning"] is False


def test_watch_state_answers_before_the_first_round(client, monkeypatch):
    """开机第一拍还没有采样结果：那是一句实话，不是 500，也不该顺手把线程拉起来。"""
    from backend.app.services.intraday_watch import IntradayWatcher

    watcher.reset()
    monkeypatch.setattr(IntradayWatcher, "keepalive", lambda self: None)
    payload = client.get("/api/realtime/intraday-watch").json()
    assert payload["status"] == "no_pool" and payload["rows"] == []
    assert payload["pool"] is None and payload["alerts"] == []
    assert "第一轮" in payload["note"]
    assert payload["sampling"]["thread_running"] is False


def test_the_ledger_survives_a_restart_of_the_watcher(watch_env):
    """确认这件事住在库里：内存清空之后「报过哪几条」仍然答得出来。"""
    _calendar(PREV, TODAY)
    _ranked_board(PREV, ["600000"])
    watch_env["holder"]["rows"] = [_quote("600000", 3.5)]
    watcher.run_round()
    with session_scope() as session:
        assert len(alerts_of(session, TODAY.isoformat())) == 1
    watcher.reset()
    payload = watch_env["client"].get("/api/realtime/intraday-watch").json()
    assert len(payload["alerts"]) == 1, "重启后那一条报警凭空消失了"
    assert payload["pinning"] is False


def test_watch_disclaimer_names_who_owns_the_rank():
    pool = WatchPool(trade_date=TODAY.isoformat(), pool_date=PREV.isoformat(), status=POOL_ABSENT, note="")
    payload = watcher._payload("no_pool", pool=pool, rows=[], note="", at=time.monotonic())
    assert "前一交易日" in payload["disclaimer"]
    assert "不是涨停概率" in payload["disclaimer"]
    assert "每只股票每一档每天只报一次" in payload["disclaimer"]
