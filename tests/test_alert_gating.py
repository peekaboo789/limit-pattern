"""v0.4.6：非交易时段（周六、节假日、午休、收盘后）任何地方都不按报警显示；交易中照常报警。榜单顺序与榜单页一致。"""

import json
from datetime import datetime

import pytest

from backend.app.core.clock import SHANGHAI
from backend.app.services import monitor_service
from tests.test_external_import_order import FIXTURE, _seed


@pytest.fixture()
def imported(client, monkeypatch):
    days = _seed()
    data = dict(FIXTURE, date=days[-1].isoformat())
    saved = client.post("/api/ai-ranking/import", json={"text": json.dumps(data, ensure_ascii=False)}).json()
    # 全部涨 7%：一旦允许报警就是 alert
    monitor_service.set_spot_fetcher(lambda cs: ([{"code": c, "close": 10.7, "pre_close": 10.0, "trade_date": days[-1].isoformat()} for c in cs], "fake"))
    yield days, saved
    monitor_service.set_spot_fetcher()


def _at(monkeypatch, moment):
    monkeypatch.setattr(monitor_service, "now", lambda: moment)


# 榜单日 2026-06-12（周五）；之后：周六 13 日、端午 19 日（休市）、15 日午休 / 收盘后 / 交易中
CASES = [
    ("saturday", datetime(2026, 6, 13, 10, 0, tzinfo=SHANGHAI), False),
    ("holiday", datetime(2026, 6, 19, 10, 0, tzinfo=SHANGHAI), False),
    ("lunch", datetime(2026, 6, 15, 12, 10, tzinfo=SHANGHAI), False),
    ("after_close", datetime(2026, 6, 15, 15, 30, tzinfo=SHANGHAI), False),
    ("in_session", datetime(2026, 6, 15, 10, 0, tzinfo=SHANGHAI), True),
]


@pytest.mark.parametrize("name,moment,fires", CASES, ids=[c[0] for c in CASES])
def test_alert_visuals_only_in_session(imported, client, monkeypatch, name, moment, fires):
    _at(monkeypatch, moment)
    snap = client.get("/api/monitor").json()
    events = client.get("/api/monitor/events", params={"since": 0}).json()
    assert snap["alerts_active"] is fires and events["alerts_active"] is fires
    levels = {item["level"] for item in snap["items"]}
    if fires:
        assert levels == {"alert"} and snap["alert_count"] == 20 and len(events["items"]) > 0
    else:
        assert levels == {"normal"} and snap["alert_count"] == 0 and snap["warn_count"] == 0
        assert events["items"] == [] and "报警暂停" in snap["price_label"]
        assert all(item["raw_level"] == "alert" for item in snap["items"])  # 数字照给，只是不按报警显示


def test_saturday_label_says_last_close(imported, client, monkeypatch):
    _at(monkeypatch, datetime(2026, 6, 13, 10, 0, tzinfo=SHANGHAI))
    assert client.get("/api/monitor").json()["price_label"] == "上一交易日 06-12 收盘（报警暂停）"


def test_monitor_uses_ranking_order(imported, client, monkeypatch):
    days, saved = imported
    _at(monkeypatch, datetime(2026, 6, 13, 10, 0, tzinfo=SHANGHAI))
    snap = client.get("/api/monitor").json()
    page = client.get("/api/ai-ranking").json()
    assert [i["code"] for i in snap["items"]] == [i["code"] for i in page["items"]]
    assert snap["items"][0]["code"] == "002365" and snap["items"][0]["rank"] == 1 and snap["items"][0]["model_score"] == 82


def test_old_external_ranking_is_reordered_for_display(client):
    """0.4.5 之前导入、按综合分存的外部榜：读出时按外部分重排。"""
    from backend.app.services.ranking_service import display_order

    old = {"trigger": "external", "items": [{"code": "600132", "rank": 1, "model_score": 78}, {"code": "002365", "rank": 19, "model_score": 82}, {"code": "600779", "rank": 2, "model_score": 74}]}
    shown = display_order(old)
    assert [i["code"] for i in shown["items"]] == ["002365", "600132", "600779"] and shown["items"][0]["rank"] == 1
    system = {"trigger": "manual", "items": old["items"]}
    assert display_order(system) is system
