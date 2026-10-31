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
    from backend.app.services.retention import purge_expired

    purge_expired()
    with session_scope() as session:
        # 补拉的 K 线在计算历史（max(池,30)+63 个交易日）内保留给 position_60，更早的才删
        from backend.app.core.config import calc_history_days
        from backend.app.services.retention import _calc_cutoff

        cut = _calc_cutoff(official[-1], max(60, calc_history_days()))
        assert session.query(CalcBar).filter(CalcBar.trade_date < cut).count() == 0


def test_failed_backfill_is_marked_and_not_scored(client):
    days = weekdays(8)
    official = days[-5:]
    _save("600000", "浦发银行", "SSE", _series(official, limit_on={2}))
    _save("000001", "平安银行", "SZSE", _series(official, limit_on=set(), start=8))

    def kline(code, start, end):
        raise RuntimeError("上游没有响应")

    set_fetcher(kline_fn=kline)
    body = client.get("/api/similarity", params={"code": "000001", "date": official[-1].isoformat()}).json()
    assert body["pool_size"] == 0
    assert body["matches"] == []
    assert body["skipped"]
    assert body["skipped"][0]["code"] == "600000"
    assert "补拉" in body["skipped"][0]["reason"]


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


def test_empty_database_starts_a_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LP_SCHEDULER", "0")
    monkeypatch.setenv("LP_AUTO_SYNC", "1")
    monkeypatch.setenv("LP_STATIC_DIR", str(tmp_path / "missing"))
    calls = []
    monkeypatch.setattr("backend.app.services.sync_service.start_sync", lambda: calls.append("go") or "task-1")
    reset_engine()
    init_db()
    from backend.app.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["data_date"] is None
    assert calls == ["go"]
    with session_scope() as session:
        session.add(
            DailyQuote(
                stock_code="600000",
                trade_date=date(2026, 6, 1),
                open=10,
                high=11,
                low=10,
                close=11,
                volume=100,
                source="test",
            )
        )
    calls.clear()
    with TestClient(create_app()):
        pass
    assert calls == []
    reset_engine()


def test_scheduler_includes_summary_after_sync(monkeypatch):
    monkeypatch.setenv("LP_SCHEDULER", "1")
    from backend.app.tasks.scheduler import scheduled_job_ids, start_scheduler, stop_scheduler

    start_scheduler()
    try:
        assert set(scheduled_job_ids()) == {"daily-sync", "daily-purge", "daily-summary", "ai-ranking-1450", "ai-ranking-1510", "morning-freeze"}
    finally:
        stop_scheduler()
