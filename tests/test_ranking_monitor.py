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
    codes = [line.split("|")[0] for line in prompt.splitlines()[2:] if "|" in line]
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
        assert snap["market_open"] is False and snap["status_text"] == "休市"
    finally:
        monitor_service.set_spot_fetcher()


def test_level_thresholds():
    assert monitor_service.level_for(None) == "normal"
    assert monitor_service.level_for(2.99) == "normal"
    assert monitor_service.level_for(3.0) == "warn"
    assert monitor_service.level_for(6.0) == "alert"
