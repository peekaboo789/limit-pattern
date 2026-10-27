"""补拉历史 K 线、统计版总结、空库自动同步、同步重试。"""

from datetime import date

from fastapi.testclient import TestClient

from backend.app.core.database import init_db, reset_engine, session_scope
from backend.app.models.tables import CalcBar, DailyQuote
from backend.app.services.sync_service import run_sync_now, set_fetcher
from tests.test_api import _save, _series, weekdays


def test_missing_prior_bars_are_fetched_for_scoring_and_then_purged(client):
    days = weekdays(8)
    official = days[-5:]
    limit_day = official[2]
    _save("600000", "浦发银行", "SSE", _series(official, limit_on={2}))
    _save("000001", "平安银行", "SZSE", _series(official, limit_on=set(), start=8))
    older = _series(days[:5], limit_on=set(), start=6)
    future = {
        "trade_date": date(2099, 1, 4),
        "open": 1,
        "high": 9,
        "low": 1,
        "close": 9,
        "pre_close": 1,
        "volume": 10,
        "amount": 10,
        "turnover_rate": 1,
    }
    asked = []

    def kline(code, start, end):
        asked.append((code, start, end))
        assert end < limit_day
        assert end <= official[-1]
        return older + [future]

    set_fetcher(kline_fn=kline)
    # 读接口只读库：补拉前打开首页，涨停前不够三根的那只列进「跳过」，一次行情源都不敲。
    before = client.get("/api/scan", params={"date": official[-1].isoformat(), "top": 20}).json()
    assert asked == [], "GET /api/scan 去敲了行情源"
    assert [item["code"] for item in before["skipped"]] == ["600000"]
    assert "只有 2 根" in before["skipped"][0]["reason"]
    again = client.get("/api/similarity", params={"code": "000001", "date": official[-1].isoformat()})
    assert again.status_code == 200 and asked == [], "GET /api/similarity 去敲了行情源"

    # 补拉是写路径（同步收尾那一步）的事。
    from backend.app.services.sync_service import _backfill_pass

    assert _backfill_pass() == ""
    body = client.get("/api/scan", params={"date": official[-1].isoformat(), "top": 20}).json()
    assert body["skipped"] == []
    assert body["pool_size"] == 1
    matched = [item for item in body["items"] if item["code"] == "000001"]
    assert matched and matched[0]["best_match"]["code"] == "600000"
    assert asked and asked[0][0] == "600000"
    with session_scope() as session:
        assert session.query(DailyQuote).filter(DailyQuote.trade_date == days[0]).count() == 0
        assert session.query(CalcBar).filter(CalcBar.trade_date == days[0]).count() == 1
        assert session.query(CalcBar).filter(CalcBar.trade_date > official[-1]).count() == 0
    from datetime import timedelta

    from backend.app.services.retention import calc_cutoff, purge_expired

    # 一根远在补拉回看之外的老线：保留窗口里谁都用不上它，清理要删。
    stale_day = calc_cutoff(official[0]) - timedelta(days=1)
    with session_scope() as session:
        session.add(CalcBar(stock_code="600000", trade_date=stale_day, open=1, high=1, low=1, close=1, pre_close=1, volume=1, amount=1, turnover_rate=1))
    purge_expired()
    with session_scope() as session:
        assert session.query(CalcBar).filter(CalcBar.trade_date < calc_cutoff(official[0])).count() == 0
        kept = session.query(CalcBar).filter(CalcBar.trade_date < official[0]).count()
    expected = len([item for item in older if item["trade_date"] < official[0]])
    assert expected and kept == expected, f"清理把保留窗口里那只涨停票的涨停前三根删了：{kept}/{expected}"
    # 清理之后首页照样凑得齐，也不再去敲行情源（盘后 17:40 清理在同步之后跑，删了就得等明天）。
    asked.clear()
    after = client.get("/api/scan", params={"date": official[-1].isoformat(), "top": 20}).json()
    assert after["skipped"] == [] and after["pool_size"] == 1
    assert asked == []


def test_failed_backfill_is_marked_and_not_scored(client):
    days = weekdays(8)
    official = days[-5:]
    _save("600000", "浦发银行", "SSE", _series(official, limit_on={2}))
    _save("000001", "平安银行", "SZSE", _series(official, limit_on=set(), start=8))

    def kline(code, start, end):
        raise RuntimeError("上游没有响应")

    set_fetcher(kline_fn=kline)
    from backend.app.services.sync_service import _backfill_pass

    note = _backfill_pass()
    assert "1 只涨停票补不齐" in note, note
    body = client.get("/api/similarity", params={"code": "000001", "date": official[-1].isoformat()}).json()
    assert body["pool_size"] == 0
    assert body["matches"] == []
    assert body["skipped"]
    assert body["skipped"][0]["code"] == "600000"
    assert "需要 3 根" in body["skipped"][0]["reason"]


def test_sync_retries_and_writes_a_statistics_summary(client):
    days = weekdays(4)
    attempts = {"n": 0}

    def kline(code, start, end):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("网络抖动")
        return _series(days, limit_on={3}, start=12)

    set_fetcher(lambda: [("600000", "浦发银行")], kline)
    result = run_sync_now()
    assert result["progress"] == 100
    assert result["saved"] == 1
    assert result["failed"] == 0
    assert attempts["n"] == 3
    assert "写入 1 只" in result["message"]
    assert "失败 0 只" in result["message"]
    assert "已写当日总结" in result["message"]
    status = client.get("/api/sync/status").json()
    assert status["phase"] == "done"
    assert status["total"] == 1
    summary = client.get("/api/summaries").json()["items"]
    assert summary
    detail = client.get(f"/api/summaries/{summary[0]['date']}").json()
    assert detail["source"] == "statistics"
    assert detail["model"] == ""
    assert "sectors" in detail and "ladder" in detail and "volume_features" in detail


def test_startup_catch_up_syncs_an_empty_database(tmp_path, monkeypatch):
    """开机那一步：空库补行情，而且抓取上限钉在「已经收盘的那一天」。

    行情齐了之后走的是另一条路（补派生层），那部分的判据在 `test_freshness_auto.py`。
    """
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LP_SCHEDULER", "0")
    monkeypatch.setenv("LP_AUTO_SYNC", "1")
    monkeypatch.setenv("LP_STATIC_DIR", str(tmp_path / "missing"))
    calls = []
    derived = []
    monkeypatch.setattr(
        "backend.app.services.sync_service.start_sync",
        lambda end=None: calls.append(end) or "task-1",
    )
    monkeypatch.setattr(
        "backend.app.services.freshness.start_derive",
        lambda moment=None: derived.append(moment),
    )
    reset_engine()
    init_db()
    from backend.app.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["data_date"] is None
    assert len(calls) == 1, "空库开机没有自动补行情"
    assert calls[0] is not None, "补行情没有钉住已收盘的那天，开盘半小时也会当成一整个交易日"
    assert derived == [], "补了行情的那条路又另起一个派生线程"
    calls.clear()
    with TestClient(create_app()):
        pass
    assert len(calls) == 1
    reset_engine()


def test_scheduler_registers_prepare_and_summary(monkeypatch):
    """四个定时任务：10:00 备固定榜输入，17:10 同步，17:40 清理，18:10 补总结。

    备榜那一步在调度里是真的挂着的那一拍——它写好了却没注册过，是「检查写了但没跑」
    那类事故的另一个版本，所以这里连钟点一起判，不许它挪到别的时刻去。
    """
    monkeypatch.setenv("LP_SCHEDULER", "1")
    import backend.app.tasks.scheduler as scheduler_module
    from backend.app.tasks.scheduler import scheduled_job_ids, start_scheduler, stop_scheduler

    start_scheduler()
    try:
        assert set(scheduled_job_ids()) == {
            "daily-sync",
            "daily-purge",
            "daily-summary",
            "daily-rank-prepare",
        }
        prepare = scheduler_module._SCHEDULER.get_job("daily-rank-prepare")
        assert "hour='10'" in str(prepare.trigger)
        assert "minute='0'" in str(prepare.trigger)
    finally:
        stop_scheduler()


def test_widening_the_window_backfills_the_newly_pooled_days_off_the_read_path(client):
    """没建过样本的库放宽对比窗口：新纳入那天的涨停前历史由后台任务补，首页 GET 仍旧不联网。"""
    import time

    days = weekdays(8)
    official = days[-5:]
    _save("600000", "浦发银行", "SSE", _series(official, limit_on={0}))
    _save("000001", "平安银行", "SZSE", _series(official, limit_on=set(), start=8))
    older = _series(days[:3], limit_on=set(), start=6)
    asked = []

    def kline(code, start, end):
        asked.append(code)
        return older

    set_fetcher(kline_fn=kline)
    params = {"date": official[-1].isoformat(), "top": 20}
    assert client.get("/api/scan", params=params).json()["pool_size"] == 0

    widened = client.put("/api/settings", json={"compare_window_trading_days": 5})
    assert widened.status_code == 200, widened.text
    assert widened.json()["window_refresh"] is not None, "没建过样本的库放宽窗口，新池子没人补拉"
    deadline = time.monotonic() + 30
    while client.get("/api/data/job").json()["running"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    state = client.get("/api/data/job").json()
    assert state["error"] == "" and state["result"]["job"] == "window_refresh"
    assert asked == ["600000"]

    asked.clear()
    body = client.get("/api/scan", params=params).json()
    assert body["skipped"] == [] and body["pool_size"] == 1
    assert asked == [], "补完之后的 GET 又去敲了行情源"
