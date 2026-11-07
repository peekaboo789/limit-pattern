"""v0.4.2 手动采集：时段判断、量能换算、标签、隔离（不进样本/回测）、同步后被替换、接口。"""

from datetime import date, datetime

import pytest

from backend.app.core.clock import SHANGHAI
from backend.app.core.database import session_scope
from backend.app.models.tables import DailyQuote
from backend.app.services import intraday_service, manual_capture
from backend.app.services.backtest_service import similarity_backtest
from tests.test_calendar_intraday_backtest import _spot
from tests.test_ranking_monitor import _seed_many

TODAY = date(2026, 6, 15)


def at(h, m, day=TODAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=SHANGHAI)


def test_classify_times():
    p = manual_capture.classify(at(14, 0))
    assert p["mode"] == "intraday" and p["elapsed"] == 180
    assert p["fraction"] == pytest.approx(intraday_service.volume_fraction(180))
    assert manual_capture.classify(at(12, 10))["elapsed"] == 120  # 午休按 2 小时
    assert manual_capture.classify(at(11, 30))["elapsed"] == 120
    close = manual_capture.classify(at(15, 40))
    assert close["mode"] == "close" and close["fraction"] == 1
    before = manual_capture.classify(at(9, 10))
    assert before["mode"] == "none" and before["fallback"] == date(2026, 6, 12)
    weekend = manual_capture.classify(at(14, 0, date(2026, 6, 13)))
    assert weekend["mode"] == "none" and weekend["fallback"] == date(2026, 6, 12)


def test_normalize_scales_volume_amount_turnover():
    f = intraday_service.volume_fraction(180)
    rows = manual_capture.normalize([{"code": "600000", "volume": 1000.0, "amount": 9000.0, "turnover_rate": 2.0}], f)
    assert rows[0]["volume"] == pytest.approx(1000 / f) and rows[0]["amount"] == pytest.approx(9000 / f)
    assert rows[0]["turnover_rate"] == pytest.approx(2.0 / f) and rows[0]["volume_estimated"]
    full = manual_capture.normalize([{"code": "600000", "volume": 1000.0, "amount": 9000.0, "turnover_rate": 2.0}], 1.0)
    assert full[0]["volume"] == 1000.0


@pytest.fixture()
def seeded(client):
    days = _seed_many()
    manual_capture.reset_state()
    intraday_service.set_bulk_fetcher(_spot(TODAY))
    yield days
    intraday_service.set_bulk_fetcher()
    manual_capture.reset_state()


def test_capture_intraday_isolated(seeded):
    with session_scope() as s:
        before_bt = similarity_backtest(s)
        n_before = s.query(DailyQuote).count()
    out = manual_capture.capture(at(14, 0))
    assert out["label"] == "盘中快照 截至 14:00" and out["kind"] == "intraday"
    assert out["elapsed_minutes"] == 180 and out["volume_estimated"]
    items = out["result"]["items"]
    assert 0 < len(items) <= 100 and "composite_score" in items[0] and "prediction_score" in items[0]
    with session_scope() as s:
        assert s.query(DailyQuote).filter(DailyQuote.trade_date == TODAY).count() == 0
        assert s.query(DailyQuote).count() == n_before
        after_bt = similarity_backtest(s)
    assert after_bt.get("days") == before_bt.get("days")
    # 快照里的涨停不进样本池：没有任何最佳匹配样本来自快照日
    assert all((i["best_match"] or {}).get("limit_date") != TODAY.isoformat() for i in items)
    second = manual_capture.capture(at(14, 20))
    saved = manual_capture.load_snapshot()
    assert saved["label"] == "盘中快照 截至 14:20" and len(saved["times"]) == 2
    assert second["date"] == TODAY.isoformat()


def test_close_label_and_no_data(seeded):
    out = manual_capture.capture(at(15, 30))
    assert out["kind"] == "close" and out["label"].endswith("收盘（15:00）") and not out["volume_estimated"]
    with pytest.raises(manual_capture.NoDataYet):
        manual_capture.capture(at(9, 0))


def test_superseded_after_official_sync(seeded, client):
    manual_capture.capture(at(14, 0))
    with session_scope() as s:
        s.add(DailyQuote(stock_code="600000", trade_date=TODAY, open=10, high=11, low=9.9, close=10.5, pre_close=10, volume=1, amount=1))
    r = client.get("/api/snapshot/manual")
    assert r.status_code == 200 and r.json()["superseded"] is True and "rows" not in r.json()
    with pytest.raises(manual_capture.NoDataYet):
        manual_capture.capture(at(14, 30))


def test_api_flow(seeded, client, monkeypatch):
    monkeypatch.setattr(manual_capture, "now", lambda: at(9, 0))
    r = client.post("/api/snapshot/capture", json={})
    assert r.status_code == 409 and "2026-06-12" in r.json()["detail"]
    fb = client.post("/api/snapshot/capture", json={"fallback": True})
    assert fb.status_code == 200 and fb.json()["kind"] == "daily"
    monkeypatch.setattr(manual_capture, "now", lambda: at(14, 0))
    start = client.post("/api/snapshot/capture", json={})
    assert start.status_code == 200
    import time
    for _ in range(300):
        st = client.get("/api/snapshot/capture/status").json()
        if not st["running"]:
            break
        time.sleep(0.1)
    assert st["phase"] == "done", st
    snap = client.get("/api/snapshot/manual").json()
    assert snap["label"] == "盘中快照 截至 14:00" and snap["superseded"] is False
    text = client.get("/api/ai-input", params={"snapshot": "manual", "format": "txt"})
    assert text.status_code == 200 and "盘中快照 截至 14:00" in text.text
    assert client.post("/api/snapshot/capture/cancel").status_code == 200


def test_latest_snapshot_survives_restart(seeded, client):
    """重启后（新进程只读文件）仍能读回最近快照，md 导出也可用。"""
    manual_capture.capture(at(14, 0))
    manual_capture.reset_state()
    r = client.get("/api/snapshot/manual")
    assert r.status_code == 200 and r.json()["label"] == "盘中快照 截至 14:00" and len(r.json()["result"]["items"]) > 0
    md = client.get("/api/ai-input", params={"snapshot": "manual", "format": "md"})
    assert md.status_code == 200 and "盘中快照 截至 14:00" in md.text
