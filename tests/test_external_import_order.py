"""v0.4.5：外部 AI 导入替换当天榜单，按外部分排序（同分保持原顺序），不丢不重排；监控用导入的 20 只。"""

import json
from datetime import datetime
from pathlib import Path

from backend.app.core.clock import SHANGHAI
from backend.app.services import monitor_service
from tests.test_api import _save, _series
from tests.test_ranking_monitor import _seed_many

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "deepseek_20261030.json").read_text(encoding="utf-8"))
CODES = [item["code"] for item in FIXTURE["items"]]


def _seed():
    days = _seed_many()
    for index, code in enumerate(CODES):
        if code == "600000":
            continue
        limit = {len(days) - 1} if code == "002365" else set()  # 002365 当天涨停
        _save(code, f"导入{index}", "SZSE" if code.startswith("00") else "SSE", _series(days, limit_on=limit, start=6 + index / 4))
    return days


def _payload(day):
    data = dict(FIXTURE, date=day.isoformat())
    return {"text": json.dumps(data, ensure_ascii=False)}


def test_fixture_has_full_fields():
    assert len(FIXTURE["items"]) == 20
    assert all(item.get("reason") and item.get("risk") and item.get("tags") for item in FIXTURE["items"])


def test_import_replaces_and_keeps_external_order(client, monkeypatch):
    days = _seed()
    day = days[-1].isoformat()
    # 先有一份系统生成的榜（模拟导入前的旧榜）
    from backend.app.core.database import session_scope
    from backend.app.services.ranking_service import generate_ranking
    from tests.test_ranking_monitor import _fake_chat

    with session_scope() as s:
        generate_ranking(s, days[-1], chat=_fake_chat)
    saved = client.post("/api/ai-ranking/import", json=_payload(days[-1])).json()
    codes = [row["code"] for row in saved["items"]]
    expected = [c for _, c in sorted(((-it["score"], i), it["code"]) for i, it in enumerate(FIXTURE["items"]))]
    assert codes == expected and codes[0] == "002365"  # 82 分排第一，没被丢也没沉底
    assert codes[4:7] == ["601187", "600029", "002271"]  # 70 分同分保持给定顺序
    assert len(codes) == 20 and saved["order"] == "external" and saved["rejected"] == []
    assert [row["rank"] for row in saved["items"]] == list(range(1, 21))
    top = saved["items"][0]
    assert top["model_score"] == 82 and top["is_limit_up"] and top["risk"] and top["tags"] == ["逆势涨停", "弱市抱团", "首板"]
    assert top["composite_score"] is None or top["model_gap"] == round(82 - top["composite_score"], 2)
    # 页面读回的是导入后的这份（替换了旧榜）
    shown = client.get("/api/ai-ranking", params={"date": day}).json()
    assert shown["trigger"] == "external" and [r["code"] for r in shown["items"]] == expected
    # 次日监控用导入的 20 只，顺序不变
    from backend.app.core.calendar import next_trading_day

    monitor_service.set_spot_fetcher(lambda cs: ([{"code": c, "close": 10.0, "pre_close": 10.0} for c in cs], "fake"))
    nxt = next_trading_day(days[-1])
    monkeypatch.setattr(monitor_service, "now", lambda: datetime(nxt.year, nxt.month, nxt.day, 10, 0, tzinfo=SHANGHAI))
    try:
        snap = client.get("/api/monitor").json()
        assert snap["ranking_date"] == day and [i["code"] for i in snap["items"]] == expected
    finally:
        monitor_service.set_spot_fetcher()


def test_rejections_are_listed(client):
    days = _seed()
    data = dict(FIXTURE, date=days[-1].isoformat())
    data["items"] = FIXTURE["items"][:3] + [{"code": "002365", "score": 1}, {"code": "abc", "score": 5}, {"code": "688999", "score": 90}]
    saved = client.post("/api/ai-ranking/import", json={"text": json.dumps(data, ensure_ascii=False)}).json()
    reasons = {r["code"]: r["reason"] for r in saved["rejected"]}
    assert "重复" in reasons["002365"] and "6 位" in reasons["abc"] and "688999" in reasons
    assert [r["code"] for r in saved["items"]] == ["002365", "600132", "600779"]
