"""手动删除 N 天前数据：自然日边界、Asia/Shanghai、保护数据、后台任务、备份失败中止、计算窗口。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from backend.app.core.clock import SHANGHAI
from backend.app.core.config import data_dir
from backend.app.core.database import session_scope
from backend.app.models.tables import AiDailySummary, AiRanking, CalcBar, DailyFeature, DailyQuote, Stock, TradingDay
from backend.app.services import backups, data_cleanup
from tests.test_api import _save, _series, weekdays


def _wait_job(job_id: str, timeout: float = 10.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while True:
        status = data_cleanup.job_status(job_id)
        if not status["running"]:
            return status
        if time.monotonic() > deadline:
            raise AssertionError(f"清理任务 {job_id} 超时未完成：{status}")
        time.sleep(0.02)


def _seed_stock(code="600000", name="浦发银行"):
    with session_scope() as session:
        if session.get(Stock, code) is None:
            session.add(Stock(code=code, name=name, exchange="SSE", board="main_board"))


def test_cutoff_is_natural_day_exactly_60_and_shanghai(monkeypatch):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    assert data_cleanup.cutoff_date(60) == date(2026, 4, 13)
    # 自然日，不受周末影响：61 天前比 60 天前早一天。
    assert data_cleanup.cutoff_date(61) == date(2026, 4, 12)

    dt_cutoff = data_cleanup._datetime_cutoff(date(2026, 4, 13))
    # Asia/Shanghai 零点 == 前一日 UTC 16 点；落库 datetime 是 naive UTC。
    assert dt_cutoff == datetime(2026, 4, 12, 16, 0)


def test_exact_60_day_boundary_is_preserved(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    days = weekdays(120, end=fixed_today)
    _seed_stock()
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100}))
    with session_scope() as session:
        session.add(AiDailySummary(trade_date=fixed_today - timedelta(days=60), payload={"x": 1}))
    # 恰好 60 天前的日线保留，61 天前的删除。
    preview = data_cleanup.preview(60)
    quotes = next(row for row in preview["tables"] if row["label"] == "行情日线")
    boundary = fixed_today - timedelta(days=60)
    with session_scope() as session:
        assert session.query(DailyQuote).filter(DailyQuote.trade_date == boundary).count() == 1
        assert session.query(DailyQuote).filter(DailyQuote.trade_date < boundary).count() == quotes["count"]
    # 每日总结表不在手动预览的表单里，默认保护。
    assert not any(row["label"] == "每日总结表" for row in preview["tables"])


def test_preview_and_execute_delete_old_data_but_protect_core_files(monkeypatch, client, tmp_path):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    root = data_dir()
    protected_files = {
        "settings.json": '{"data_cleanup_days":60}',
        "auth.json": '{"username":"admin","hash":"x"}',
        ".session_secret": "secret",
        "watchlist.json": '{"items":[]}',
        "wechat_session.json": '{"bound_user":"u"}',
        "wecom_sent.json": '{}',
    }
    for name, text in protected_files.items():
        (root / name).write_text(text, encoding="utf-8")
    old_dir = root / "exports"
    old_dir.mkdir(parents=True, exist_ok=True)
    old_file = old_dir / "2026-03-01-export.md"
    old_file.write_text("old", encoding="utf-8")
    fresh_file = old_dir / "2026-06-11-export.md"
    fresh_file.write_text("new", encoding="utf-8")

    days = weekdays(120, end=fixed_today)
    _seed_stock()
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100}))
    boundary = fixed_today - timedelta(days=60)
    with session_scope() as session:
        session.add(AiRanking(trade_date=fixed_today - timedelta(days=61), run_at="x", trigger="t", payload={}))
        session.add(AiDailySummary(trade_date=boundary, payload={"x": 1}))

    preview = data_cleanup.preview(60)
    assert preview["timezone"] == "Asia/Shanghai"
    assert preview["cutoff"] == "2026-04-13"
    ranking = next(row for row in preview["tables"] if row["label"] == "AI 前 20 榜单")
    assert ranking["count"] == 1

    result = data_cleanup.execute(60)
    assert result["deleted_files"] >= 1
    assert not old_file.exists()
    assert fresh_file.exists()
    for name in protected_files:
        assert (root / name).exists(), name

    with session_scope() as session:
        boundary = fixed_today - timedelta(days=60)
        assert session.query(DailyQuote).filter(DailyQuote.trade_date < boundary).count() == 0
        assert session.query(DailyQuote).filter(DailyQuote.trade_date == boundary).count() == 1
        assert session.query(AiRanking).count() == 0  # 手动删除允许清理 AI 前 20 榜单
        assert session.get(AiDailySummary, boundary) is not None  # 每日总结表仍保护


def test_auto_cleanup_keeps_permanent_tables_and_is_off_by_default(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    from backend.app.core import config

    config.write_settings_file({**config.read_settings_file(), "data_cleanup_auto_enabled": False, "data_cleanup_days": 60})
    assert data_cleanup.auto_cleanup_if_enabled() == {"ran": False}

    with session_scope() as session:
        session.add(AiRanking(trade_date=fixed_today - timedelta(days=61), run_at="x", trigger="t", payload={}))
        session.add(AiDailySummary(trade_date=fixed_today - timedelta(days=61), payload={"x": 1}))

    config.write_settings_file({**config.read_settings_file(), "data_cleanup_auto_enabled": True, "data_cleanup_days": 60})
    result = data_cleanup.auto_cleanup_if_enabled()
    assert result["ran"] is True
    assert "job_id" in result
    status = _wait_job(result["job_id"])
    assert status["status"] == "done"
    assert status["result"]["mode"] == "auto"
    assert "AI 前 20 榜单" in status["result"]["protected"]
    with session_scope() as session:
        assert session.query(AiRanking).count() == 1
        assert session.query(AiDailySummary).count() == 1


def test_backup_failure_aborts_before_delete(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    root = data_dir()
    days = weekdays(120, end=fixed_today)
    _seed_stock()
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100}))
    old_file = root / "exports" / "2026-03-01.txt"
    old_file.parent.mkdir(exist_ok=True)
    old_file.write_text("old", encoding="utf-8")

    def broken_backup(label):
        raise OSError("备份磁盘不可用")

    monkeypatch.setattr(backups, "make_backup", broken_backup)
    try:
        data_cleanup.execute(60)
    except Exception as exc:
        assert "备份磁盘不可用" in str(exc)
    else:
        raise AssertionError("备份失败应当中止删除")

    with session_scope() as session:
        assert session.query(DailyQuote).count() > 0
    assert old_file.exists()


def test_invalid_backup_aborts_before_delete(monkeypatch, client, tmp_path):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    days = weekdays(120, end=fixed_today)
    _seed_stock()
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100}))

    class FakeBackup:
        name = "fake-backup"

    monkeypatch.setattr(backups, "make_backup", lambda label: FakeBackup())
    try:
        data_cleanup.execute(60)
    except Exception as exc:
        assert "备份" in str(exc)
    else:
        raise AssertionError("备份校验失败应当中止删除")

    with session_scope() as session:
        assert session.query(DailyQuote).count() > 0


def test_calc_window_archive_is_kept_for_position_60(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    from backend.app.core.config import calc_history_days
    from backend.app.core.calendar import previous_trading_day

    days = weekdays(120, end=fixed_today)
    _seed_stock()
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100}))
    latest = days[-1]
    calc_cutoff = latest
    for _ in range(calc_history_days() - 1):
        calc_cutoff = previous_trading_day(calc_cutoff)

    preview = data_cleanup.preview(60)
    assert preview["calc_cutoff"] is not None
    assert preview["calc_protected_archive"] > 0
    result = data_cleanup.execute(60)
    assert result["deleted"]["archived_to_calc_bars"] > 0
    with session_scope() as session:
        # 计算窗口起点前不再保留正式/补来日线；窗口内转存到 calc_bars。
        assert session.query(DailyQuote).filter(DailyQuote.trade_date < result["cutoff"]).count() == 0
        assert session.query(CalcBar).filter(CalcBar.trade_date < calc_cutoff).count() == 0
        assert session.query(CalcBar).filter(CalcBar.trade_date >= calc_cutoff).count() > 0


def test_start_job_and_status_are_background_and_single_runner(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    days = weekdays(120, end=fixed_today)
    _seed_stock()
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100}))
    with session_scope() as session:
        session.add(AiRanking(trade_date=fixed_today - timedelta(days=61), run_at="x", trigger="t", payload={}))

    job = data_cleanup.start_job(60, mode="manual")
    assert job["running"] is True
    assert job["phase"] in {"queued", "backup", "tables", "files", "vacuum"}
    try:
        data_cleanup.start_job(60, mode="manual")
    except RuntimeError as exc:
        assert "已有删除任务" in str(exc)
    else:
        raise AssertionError("同一时间只能有一个清理任务")

    status = _wait_job(job["job_id"])
    assert status["status"] == "done"
    assert status["phase"] == "vacuum"
    assert status["progress"] == 100
    assert status["elapsed_seconds"] >= 0
    assert status["result"]["mode"] == "manual"


def test_execute_route_returns_job_id_and_status_route(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    days = weekdays(120, end=fixed_today)
    _seed_stock()
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100}))

    response = client.post("/api/data-cleanup/execute", json={"days": 60, "confirm": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"]
    status = _wait_job(body["job_id"])
    assert status["status"] == "done"

    response = client.get(f"/api/data-cleanup/jobs/{body['job_id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["progress"] == 100

    missing = client.get("/api/data-cleanup/jobs/does-not-exist")
    assert missing.status_code == 404


def test_vacuum_skips_when_disk_does_not_have_room(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    db_path = data_dir() / "lp.db"
    _seed_stock()
    with session_scope() as session:
        session.add(DailyQuote(stock_code="600000", trade_date=fixed_today, open=1, high=2, low=0.5, close=1.5))
    db_size = data_cleanup._db_size(db_path)
    monkeypatch.setattr(data_cleanup, "_free_disk_bytes", lambda path: max(0, db_size - 1))
    vacuum = data_cleanup._vacuum()
    assert vacuum["vacuum"] is False
    assert vacuum.get("skipped_disk_space") is True
    assert "磁盘空间不足" in vacuum["reason"]


def test_purge_preserves_trading_days_and_pattern_features(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    from backend.app.core.calendar import previous_trading_day
    from backend.app.core.config import calc_history_days

    days = weekdays(120, end=fixed_today)
    _seed_stock()
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100}))
    with session_scope() as session:
        for day in days:
            if session.get(TradingDay, day) is None:
                session.add(TradingDay(trade_date=day, is_open=True, limit_up_count=0))
        session.add(DailyFeature(stock_code="600000", trade_date=fixed_today - timedelta(days=90), features={"x": 1}))
        session.add(DailyFeature(stock_code="600000", trade_date=fixed_today, features={"x": 2}))
    before_trading_days = None
    with session_scope() as session:
        before_trading_days = session.query(TradingDay).count()
    assert before_trading_days == len(days)

    preview = data_cleanup.preview(60)
    assert not any(row["label"] == "交易日历" for row in preview["tables"])

    result = data_cleanup.execute(60)
    with session_scope() as session:
        assert session.query(TradingDay).count() == before_trading_days
        assert session.query(DailyFeature).filter(DailyFeature.trade_date == fixed_today).count() == 1
        # 旧特征也要保护到 calc_cutoff，不能只按 retention_days 的 60 自然日切。
        calc_cutoff = data_cleanup._calc_cutoff(session)
        assert session.query(DailyFeature).filter(DailyFeature.trade_date < calc_cutoff).count() == 0
        assert session.query(DailyFeature).count() > 0
    assert result["deleted"].get("交易日历") in (None, 0)


def test_pattern_similarity_is_identical_before_and_after_purge(monkeypatch, client):
    fixed_today = date(2026, 6, 12)
    monkeypatch.setattr(data_cleanup, "today", lambda: fixed_today)
    days = weekdays(120, end=fixed_today)
    _seed_stock("600000", "浦发银行")
    _seed_stock("000001", "平安银行")
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={100, 110}))
    _save("000001", "平安银行", "SZSE", _series(days, limit_on=set(), start=8.0))

    from backend.app.services.similarity_service import scan_market

    as_of = days[-1]
    with session_scope() as session:
        before = scan_market(session, as_of, top=10, min_score=0, details=False)
    with session_scope() as session:
        before_trading_days = session.query(TradingDay).count()
    # 相似度计算的会话缓存不要串到下一次。
    data_cleanup.execute(60)
    with session_scope() as session:
        after = scan_market(session, as_of, top=10, min_score=0, details=False)
        after_trading_days = session.query(TradingDay).count()

    assert before["pool_dates"] == after["pool_dates"]
    assert before["pool_size"] == after["pool_size"]
    assert [item["code"] for item in before["items"]] == [item["code"] for item in after["items"]]
    assert [item["max_similarity"] for item in before["items"]] == [item["max_similarity"] for item in after["items"]]
    assert after_trading_days == before_trading_days
