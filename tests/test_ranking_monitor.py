"""AI 前 20 榜单（AI 用假的）和盘中监控。"""

import json
from datetime import datetime

from backend.app.core.clock import SHANGHAI
from backend.app.core.config import rankings_dir
from backend.app.core.database import session_scope
from backend.app.models.tables import AiRanking
from backend.app.services import monitor_service
from backend.app.services.ranking_service import generate_ranking
from tests.test_api import _save, _series, weekdays


def _seed_many():
    days = weekdays(8)
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={5, 6, 7}))
    for index in range(30):
        code = f"{600100 + index}"
        _save(code, f"股票{index}", "SSE", _series(days, limit_on=set(), start=5 + index / 3))
    return days


def _fake_chat(prompt: str) -> str:
    import re
    codes = re.findall(r"^(\d{6}) ", prompt, flags=re.M)
    return json.dumps({"items": [{"code": code, "score": int(code) % 97, "reason": "量价接近"} for code in codes]})


def test_ranking_needs_ai(client):
    _seed_many()
    response = client.post("/api/ai-ranking/generate", json={})
    assert response.status_code == 409
    assert client.get("/api/ai-ranking").status_code == 404


def test_ranking_keeps_top20_and_persists(client):
    days = _seed_many()
    with session_scope() as session:
        payload = generate_ranking(session, days[-1], chat=_fake_chat)
    items = payload["items"]
    assert len(items) == 20
    assert payload["candidate_count"] <= 100
    assert [item["rank"] for item in items] == list(range(1, 21))
    assert all(items[i]["ai_score"] >= items[i + 1]["ai_score"] for i in range(19))
    assert all(0 <= item["ai_score"] <= 100 and item["reason"] for item in items)
    assert payload["disclaimer"] == "相似度不是涨停概率"
    assert (rankings_dir() / f"{days[-1].isoformat()}.json").is_file()
    got = client.get("/api/ai-ranking", params={"date": days[-1].isoformat()}).json()
    assert got["items"] == items
    assert client.get("/api/ai-ranking/dates").json()["dates"] == [days[-1].isoformat()]
    text = client.get("/api/export", params={"type": "ai_ranking", "date": days[-1].isoformat()})
    assert text.status_code == 200
    assert "AI评分" in text.content.decode("utf-8-sig")


def test_monitor_levels_and_events_once(client, monkeypatch):
    days = _seed_many()
    with session_scope() as session:
        generate_ranking(session, days[-1], chat=_fake_chat)
        board = session.query(AiRanking).first().payload["items"]
    codes = [item["code"] for item in board]
    pct = {codes[0]: 6.5, codes[1]: 3.2}

    def fetch(requested):
        return [{"code": c, "close": 10 * (1 + pct.get(c, 1.0) / 100), "pre_close": 10.0} for c in requested], "fake"

    monitor_service.set_spot_fetcher(fetch)
    open_moment = datetime(2026, 6, 15, 10, 0, tzinfo=SHANGHAI)  # 周一
    monkeypatch.setattr(monitor_service, "now", lambda: open_moment)
    try:
        snap = client.get("/api/monitor").json()
        assert snap["market_open"] is True
        assert snap["ranking_date"] == days[-1].isoformat()
        levels = {item["code"]: item["level"] for item in snap["items"]}
        assert levels[codes[0]] == "alert"
        assert levels[codes[1]] == "warn"
        assert levels[codes[2]] == "normal"
        first = client.get("/api/monitor/events", params={"since": 0}).json()
        kinds = sorted((item["code"], item["level"]) for item in first["items"])
        assert kinds == sorted([(codes[0], "warn"), (codes[0], "alert"), (codes[1], "warn")])
        monitor_service.reset_cache()
        again = client.get("/api/monitor/events", params={"since": first["last_id"]}).json()
        assert again["items"] == []
        closed = datetime(2026, 6, 13, 10, 0, tzinfo=SHANGHAI)  # 周六
        monkeypatch.setattr(monitor_service, "now", lambda: closed)
        snap = client.get("/api/monitor").json()
        assert snap["market_open"] is False and snap["status_text"] == "非交易日，报警暂停"
    finally:
        monitor_service.set_spot_fetcher()


def test_level_thresholds():
    assert monitor_service.level_for(None) == "normal"
    assert monitor_service.level_for(2.99) == "normal"
    assert monitor_service.level_for(3.0) == "warn"
    assert monitor_service.level_for(6.0) == "alert"


def test_alert_phase_with_frozen_clock():
    def at(y, m, d, hh, mm, **settings):
        return monitor_service.alert_phase(datetime(y, m, d, hh, mm, tzinfo=SHANGHAI), settings)

    assert at(2026, 6, 15, 9, 30) == {"phase": "trading", "alerts_active": True, "text": "交易中"}
    assert at(2026, 6, 15, 14, 59)["alerts_active"] is True
    assert at(2026, 6, 15, 9, 10)["text"] == "未开盘，报警暂停"
    assert at(2026, 6, 15, 9, 20)["alerts_active"] is False
    assert at(2026, 6, 15, 9, 20, alert_include_auction=True)["alerts_active"] is True
    assert at(2026, 6, 15, 9, 27)["phase"] == "pre_open"
    assert at(2026, 6, 15, 11, 30) == {"phase": "lunch", "alerts_active": False, "text": "午休，报警暂停"}
    assert at(2026, 6, 15, 15, 0) == {"phase": "closed", "alerts_active": False, "text": "已收盘，报警暂停"}
    assert at(2026, 6, 13, 10, 0)["text"] == "非交易日，报警暂停"  # 周六
    assert at(2026, 10, 1, 10, 0)["phase"] == "non_trading_day"  # 国庆
    assert at(2026, 6, 15, 16, 0, alert_trading_hours_only=False)["alerts_active"] is True


def test_no_alerts_or_quotes_outside_sessions_and_no_replay_after_restart(client, monkeypatch):
    days = _seed_many()
    with session_scope() as session:
        generate_ranking(session, days[-1], chat=_fake_chat)
        codes = [item["code"] for item in session.query(AiRanking).first().payload["items"]]
    calls = []

    def fetch(requested):
        calls.append(1)
        return [{"code": c, "close": 10.7 if c == codes[0] else 10.0, "pre_close": 10.0} for c in requested], "fake"

    monitor_service.set_spot_fetcher(fetch)
    clock = {"t": datetime(2026, 6, 15, 15, 30, tzinfo=SHANGHAI)}  # 收盘后，行情里还挂着 +7%
    monkeypatch.setattr(monitor_service, "now", lambda: clock["t"])
    try:
        events = client.get("/api/monitor/events", params={"since": 0}).json()
        assert events["items"] == [] and events["alerts_active"] is False and calls == []  # 不拉行情
        snap = client.get("/api/monitor").json()
        assert snap["status_text"] == "已收盘，报警暂停" and snap["alerts_active"] is False
        assert snap["alert_count"] == 0  # 只显示最后数字，不亮红框
        assert {i["code"]: i["pct"] for i in snap["items"]}[codes[0]] is not None
        assert client.get("/api/monitor/events", params={"since": 0}).json()["items"] == []

        clock["t"] = datetime(2026, 6, 15, 12, 0, tzinfo=SHANGHAI)
        assert client.get("/api/monitor").json()["status_text"] == "午休，报警暂停"
        assert client.get("/api/monitor/events", params={"since": 0}).json()["items"] == []

        clock["t"] = datetime(2026, 6, 15, 14, 0, tzinfo=SHANGHAI)
        monitor_service.reset_cache()
        live = client.get("/api/monitor/events", params={"since": 0}).json()
        assert {(i["code"], i["level"]) for i in live["items"]} == {(codes[0], "warn"), (codes[0], "alert")}
        monitor_service.reset_cache()
        assert client.get("/api/monitor/events", params={"since": 0}).json()["last_id"] == live["last_id"]  # 每只每天只记一次

        clock["t"] = datetime(2026, 6, 15, 15, 1, tzinfo=SHANGHAI)  # 收盘后"重启"，游标从 0 开始也不补发
        assert client.get("/api/monitor/events", params={"since": 0}).json()["items"] == []
        assert client.get("/api/monitor").json()["alert_count"] == 0
    finally:
        monitor_service.set_spot_fetcher()


def test_trading_hours_toggle_round_trip(client):
    saved = client.put("/api/settings", json={"alert_trading_hours_only": False, "alert_include_auction": True}).json()
    assert saved["alert_trading_hours_only"] is False and saved["alert_include_auction"] is True
    assert client.put("/api/settings", json={"alert_trading_hours_only": "yes"}).status_code == 422
    assert client.get("/api/settings").json()["alert_trading_hours_only"] is False
