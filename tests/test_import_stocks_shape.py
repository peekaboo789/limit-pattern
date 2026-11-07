"""v0.4.8：外部 AI 导入接受 {"stocks":[...], title, market_sentiment, market_risk, disclaimer} 这种格式。"""

import json
from datetime import datetime
from pathlib import Path

from backend.app.core.clock import SHANGHAI
from backend.app.services import monitor_service
from tests.test_external_import_order import _seed

RAW = (Path(__file__).parent / "fixtures" / "dual_model_20261030.json").read_text(encoding="utf-8")
ORDER = ["600132", "002839", "601187", "000598", "600000", "600029", "601169"]


def _import(client, text, day):
    return client.post("/api/ai-ranking/import", json={"text": text.replace("2026-10-30", day)})


def test_stocks_shape_priority_meta_and_monitor(client, monkeypatch):
    days = _seed()
    r = _import(client, RAW, days[-1].isoformat())
    assert r.status_code == 200, r.text
    saved = r.json()
    assert [i["code"] for i in saved["items"]] == ORDER and len(saved["items"]) == 7
    first = saved["items"][0]
    assert first["model_score"] == 87 and first["extra_scores"] == {"deepseek_score": 78} and first["priority"] == 1
    assert first["reason"] and first["risk"]
    meta = saved["external_meta"]
    assert meta["title"].startswith("第一梯队") and meta["market_risk"]["broken_rate"] == 39.7 and "回测" in meta["disclaimer"]
    assert meta["market_sentiment"].startswith("弱势市场")
    # 测试库里名称是「导入N」，与 JSON 不符：提醒但仍按代码导入；600000 浦发银行一致不提醒
    warned = {w["code"] for w in saved["name_warnings"]}
    assert "600132" in warned and "600000" not in warned and first["name_warning"]
    from backend.app.core.calendar import next_trading_day

    monitor_service.set_spot_fetcher(lambda cs: ([{"code": c, "close": 10.0, "pre_close": 10.0} for c in cs], "fake"))
    nxt = next_trading_day(days[-1])
    monkeypatch.setattr(monitor_service, "now", lambda: datetime(nxt.year, nxt.month, nxt.day, 10, 0, tzinfo=SHANGHAI))
    try:
        snap = client.get("/api/monitor").json()
        assert [i["code"] for i in snap["items"]] == ORDER
    finally:
        monitor_service.set_spot_fetcher()


def test_tolerant_wrapping(client):
    days = _seed()
    messy = "\ufeff说明文字\n```json\n" + RAW.replace('"code":"600132"', '"code"：“600132”').replace("}]", "},]") + "\n```\n"
    r = _import(client, messy, days[-1].isoformat())
    assert r.status_code == 200, r.text
    assert [i["code"] for i in r.json()["items"]] == ORDER


def test_watchlist_import_accepts_this_json(client):
    _seed()
    preview = client.post("/api/watchlist/import/preview", json={"text": RAW}).json()
    assert [m["code"] for m in preview["matched"]] == ORDER
