"""导出前 100 给外部 AI，以及导入外部 AI 的前 20 JSON。"""

import json
from datetime import datetime

from backend.app.core.clock import SHANGHAI
from backend.app.services import monitor_service
from backend.app.services.ai_export import parse_external
from backend.app.services.ranking_service import SYSTEM_PROMPT
from tests.test_ranking_monitor import _seed_many


def test_ai_input_export_contents(client):
    days = _seed_many()
    response = client.get("/api/ai-input", params={"date": days[-1].isoformat()})
    assert response.status_code == 200
    text = response.content.decode("utf-8")
    assert f"评估日 {days[-1].isoformat()}" in text and "程序版本" in text and "样本池" in text
    assert "正式日线" in text and "市场环境" in text and SYSTEM_PROMPT in text
    lines = [line for line in text.splitlines() if line[:6].isdigit()]
    assert 20 <= len(lines) <= 100 and all("近5日" in line and "样本" in line and "连板" in line for line in lines)
    assert '"items"' in text.split("输出要求")[-1]
    assert "attachment" in response.headers["content-disposition"]
    md = client.get("/api/export", params={"type": "ai_input_md", "date": days[-1].isoformat()})
    assert md.status_code == 200 and md.content.decode("utf-8").startswith("# ") and "```text" in md.text


def test_parse_external_is_defensive():
    text = """好的，以下是结果：
```json
{"date":"2026-06-12","model":"gpt-x","items":[
 {"code":"600000.SH","score":"81分","sub":{"kline":"80","volume":70},"risk_deduct":-4,"reason":"平台突破","tags":"放量突破、均线多头"},
 {"code":600100,"score":75},
 {"code":"sh600101","总分":70,"理由":"缩量"},
 {"code":"abc","score":99},
 {"code":"600000","score":10},
]}
```"""
    scores, meta = parse_external(text)
    assert meta["model"] == "gpt-x"
    assert list(scores) == ["600000", "600100", "600101"]
    assert scores["600000"]["score"] == 81 and scores["600000"]["risk_deduct"] == 4
    assert scores["600000"]["tags"] == ["放量突破", "均线多头"] and scores["600101"]["reason"] == "缩量"
    bare, _ = parse_external('[{"code":"600102","score":60}]')
    assert list(bare) == ["600102"]


def test_import_external_ranking_is_monitored(client, monkeypatch):
    days = _seed_many()
    items = [{"code": f"{600100 + i}", "score": 90 - i, "reason": f"理由{i}"} for i in range(25)]
    items.append({"code": "688999", "score": 99})  # 本地不存在
    body = {"text": json.dumps({"date": days[-1].isoformat(), "model": "外部模型", "items": items}, ensure_ascii=False)}
    saved = client.post("/api/ai-ranking/import", json=body).json()
    assert saved["trigger"] == "external" and saved["model"] == "外部模型"
    assert len(saved["items"]) == 20 and saved["items"][0]["code"] == "600100" and saved["items"][0]["rank"] == 1
    assert saved["dropped_unknown"] == ["688999"] and saved["date"] == days[-1].isoformat()
    assert client.get("/api/ai-ranking", params={"date": days[-1].isoformat()}).json()["trigger"] == "external"
    assert client.post("/api/ai-ranking/import", json={"text": "没有 JSON"}).status_code == 422

    monitor_service.set_spot_fetcher(lambda codes: ([{"code": c, "close": 10.0, "pre_close": 10.0} for c in codes], "fake"))
    from backend.app.core.calendar import next_trading_day

    nxt = next_trading_day(days[-1])
    monkeypatch.setattr(monitor_service, "now", lambda: datetime(nxt.year, nxt.month, nxt.day, 10, 0, tzinfo=SHANGHAI))
    try:
        snap = client.get("/api/monitor").json()
        assert snap["ranking_date"] == days[-1].isoformat() and snap["items"][0]["code"] == "600100"
    finally:
        monitor_service.set_spot_fetcher()
