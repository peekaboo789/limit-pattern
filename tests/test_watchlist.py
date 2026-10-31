"""v0.4.8 自选股：代码 / 汉字 / 拼音查找、导入解析、持久化、删除、报警时段。"""

from datetime import datetime

import pytest

from backend.app.core.clock import SHANGHAI
from backend.app.core.database import session_scope
from backend.app.services import monitor_service
from backend.app.services import watchlist_service as wl
from tests.test_api import _save, _series, weekdays


def _seed():
    days = weekdays(8)
    for code, name in [("600132", "重庆啤酒"), ("600000", "浦发银行"), ("601169", "北京银行"), ("601818", "光大银行"),
                       ("000001", "平安银行"), ("002365", "永安药业"), ("300750", "宁德时代")]:
        _save(code, name, "SZSE" if code.startswith(("0", "3")) else "SSE", _series(days, limit_on=set()))
    return days


def test_initials():
    assert wl.initials("重庆啤酒") == "cqpj" and wl.initials("浦发银行") == "pfyh" and wl.initials("*ST东园") == "stdy"


def test_search_code_name_pinyin(client):
    _seed()
    with session_scope() as s:
        assert [r["code"] for r in wl.search(s, "600132")] == ["600132"]
        assert [r["code"] for r in wl.search(s, "sh600132")] == ["600132"]
        assert [r["code"] for r in wl.search(s, "600132.SH")] == ["600132"]
        assert wl.search(s, "重庆啤")[0]["code"] == "600132"
        assert wl.search(s, "cqpj")[0]["code"] == "600132"
        assert {r["code"] for r in wl.search(s, "银行")} == {"600000", "601169", "601818", "000001"}
        star = wl.search(s, "688981")
        assert star[0]["star"] and star[0]["board"] == "科创板"
    r = client.get("/api/watchlist/search", params={"q": "宁德"}).json()
    assert r["items"][0]["code"] == "300750" and r["items"][0]["board"] == "创业板"


def test_import_parsing_mixed(client):
    _seed()
    client.post("/api/watchlist", json={"codes": ["601169"]})
    text = "代码\t名称\nSH600132\t重庆啤酒\n600000 浦发银行\nsz000001, 永安药业，宁德\n银行\n不存在的股\n600132\n601169\n999999\n688981"
    p = client.post("/api/watchlist/import/preview", json={"text": text}).json()
    assert [m["code"] for m in p["matched"]] == ["600132", "600000", "000001", "002365", "300750", "688981"]
    assert [a["token"] for a in p["ambiguous"]] == ["银行"] and len(p["ambiguous"][0]["candidates"]) >= 3
    assert {n["token"] for n in p["not_found"]} == {"不存在的股", "999999"}
    reasons = {d["code"]: d["reason"] for d in p["duplicates"]}
    assert reasons == {"600132": "文本里重复", "601169": "已在自选"}


def test_persist_delete_note_reorder_export(client):
    _seed()
    r = client.post("/api/watchlist", json={"items": [{"code": "600132"}, {"code": "sz000001", "note": "观察"}, {"code": "688981"}]}).json()
    assert r["added"] == ["600132", "000001", "688981"] and r["items"][0]["name"] == "重庆啤酒"
    assert client.post("/api/watchlist", json={"codes": ["600132"]}).json()["duplicates"] == ["600132"]
    assert [x["code"] for x in wl.load()] == ["600132", "000001", "688981"]  # 存在文件里，重启也在
    client.post("/api/watchlist/note", json={"code": "600132", "note": "龙头"})
    client.post("/api/watchlist/reorder", json={"codes": ["688981", "600132", "000001"]})
    items = client.get("/api/watchlist").json()["items"]
    assert [x["code"] for x in items] == ["688981", "600132", "000001"] and items[1]["note"] == "龙头"
    exp = client.get("/api/watchlist/export", params={"format": "csv"})
    assert exp.content.startswith(b"\xef\xbb\xbf") and "重庆啤酒" in exp.content.decode("utf-8-sig")
    assert client.get("/api/watchlist/export").content.startswith(b"\xef\xbb\xbf")
    client.post("/api/watchlist/delete", json={"codes": ["600132", "000001"]})
    assert [x["code"] for x in wl.load()] == ["688981"]
    client.post("/api/watchlist/delete", json={"all": True})
    assert wl.load() == []


CASES = [
    ("saturday", datetime(2026, 6, 13, 10, 0, tzinfo=SHANGHAI), False),
    ("holiday", datetime(2026, 6, 19, 10, 0, tzinfo=SHANGHAI), False),
    ("lunch", datetime(2026, 6, 15, 12, 10, tzinfo=SHANGHAI), False),
    ("after_close", datetime(2026, 6, 15, 15, 30, tzinfo=SHANGHAI), False),
    ("in_session", datetime(2026, 6, 15, 10, 0, tzinfo=SHANGHAI), True),
]


@pytest.mark.parametrize("name,moment,fires", CASES, ids=[c[0] for c in CASES])
def test_watchlist_alert_gating(client, monkeypatch, name, moment, fires):
    _seed()
    client.post("/api/watchlist", json={"codes": ["600132", "000001", "688981"]})
    calls = []

    def fetch(codes):
        calls.append(list(codes))
        return [{"code": c, "close": 11.0 if c == "600132" else 10.4, "pre_close": 10.0, "high": 11.0, "low": 9.9, "volume": 1000.0,
                 "trade_date": "2026-06-12"} for c in codes], "fake"

    wl._QCACHE.update(key=None)
    monitor_service.set_spot_fetcher(fetch)
    monkeypatch.setattr(wl, "now", lambda: moment)
    try:
        snap = client.get("/api/watchlist/monitor").json()
    finally:
        monitor_service.set_spot_fetcher()
    assert len(calls) == 1 and sorted(calls[0]) == ["000001", "600132", "688981"]  # 一次批量请求
    by = {r["code"]: r for r in snap["items"]}
    assert by["600132"]["is_limit_up"] and by["600132"]["pct"] == 10.0 and by["600132"]["high"] == 11.0
    assert by["688981"]["star"] and by["688981"]["composite_score"] is None
    assert snap["alerts_active"] is fires
    if fires:
        assert by["600132"]["level"] == "alert" and by["000001"]["level"] == "warn" and snap["alert_count"] == 1
    else:
        assert {r["level"] for r in snap["items"]} == {"normal"} and "报警暂停" in snap["price_label"]


def test_watchlist_alert_toggle_off(client, monkeypatch):
    _seed()
    client.post("/api/watchlist", json={"codes": ["600132"]})
    client.put("/api/settings", json={"watchlist_alerts": False})
    wl._QCACHE.update(key=None)
    monitor_service.set_spot_fetcher(lambda cs: ([{"code": c, "close": 11.0, "pre_close": 10.0} for c in cs], "fake"))
    monkeypatch.setattr(wl, "now", lambda: datetime(2026, 6, 15, 10, 0, tzinfo=SHANGHAI))
    try:
        snap = client.get("/api/watchlist/monitor").json()
    finally:
        monitor_service.set_spot_fetcher()
    assert snap["alerts_active"] is False and snap["items"][0]["level"] == "normal" and snap["alerts_enabled"] is False
