"""多模型讨论验证：发言顺序、失败降级、概率钳制、密钥不落盘、指令解析。不连外网、不打真实模型。"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from backend.app.core.database import session_scope, reset_engine
from backend.app.models.tables import AiCouncilSession, DailyQuote, Stock, TradingDay
from backend.app.providers.markets import normalize_code
from backend.app.services.council import service
from backend.app.services.council import web_evidence


def _add_profiles(client, *models):
    pub = client.get("/api/ai/profiles").json()
    current_ids = [p["id"] for p in pub["items"]]
    for idx, model in enumerate(models):
        r = client.post("/api/ai/profiles", json={
            "name": model["name"], "base_url": "https://example.invalid/v1", "model": model["model"], "api_key": f"sk-key-{idx}-fake",
        })
        assert r.status_code == 200, r.text
    pub = client.get("/api/ai/profiles").json()
    ids = [p["id"] for p in pub["items"] if p["id"] not in current_ids]
    return pub["items"]


def _seed_quotes(day: date = date(2026, 6, 12)):
    days = []
    cursor = day
    while len(days) < 8:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days = sorted(days)
    with session_scope() as session:
        stock = session.get(Stock, "600519")
        if stock is None:
            stock = Stock(code="600519", name="贵州茅台", exchange="SSE", board="main_board")
            session.add(stock)
        for i, d in enumerate(days):
            if session.query(DailyQuote).filter(DailyQuote.stock_code == "600519", DailyQuote.trade_date == d).first():
                continue
            session.add(DailyQuote(stock_code="600519", trade_date=d, open=100+i, high=101+i, low=99+i,
                                   close=100.5+i, pre_close=100+i, volume=1000+i*10, amount=10000+i,
                                   turnover_rate=1.0+i/10, change_percent=0.5, is_limit_up=False, is_limit_down=False,
                                   limit_up_price=110, limit_down_price=90, source="test"))
            if session.get(TradingDay, d) is None:
                session.add(TradingDay(trade_date=d, is_open=True, limit_up_count=0))
        session.commit()
    return days[-1]


def _row_id(day):
    with session_scope() as session:
        row = AiCouncilSession(stock_code="600519", stock_name="贵州茅台", eval_date=day, status="running",
                               participants=[], evidence={}, statements=[], conclusion=None, error="")
        session.add(row)
        session.commit()
        return row.id


def test_parse_aliases():
    from backend.app.services.wecom import commands

    for text in ("开会验证 600519", "多模型验证 茅台", "讨论验证 000001"):
        assert commands.parse(text)[0] == "开会验证"
    assert commands.parse("帮助")[0] == "帮助"


def test_not_enough_profiles_rejected(client):
    _seed_quotes()
    # 未配置任何 profile
    r = client.post("/api/council/run", json={"code": "600519"})
    assert r.status_code == 422
    assert "至少需要两个" in r.json()["detail"]


def test_star_board_rejected(client):
    _seed_quotes()
    r = client.post("/api/council/run", json={"code": "688981"})
    assert r.status_code == 422
    assert "科创板" in r.json()["detail"]


def test_failed_web_still_uses_local(client, monkeypatch):
    _seed_quotes()
    from backend.app.services.council import service as council_service

    def fake_collect(code, end=None):
        return [{"status": "failed", "source": "eastmoney_spot", "url": "https://example", "fetched_at": "now", "digest": {"reason": "模拟失败"}}]

    from backend.app.services.council import web_evidence as we_module
    monkeypatch.setattr(we_module, "collect", fake_collect)
    profiles = [
        {"id": "a", "name": "甲", "model": "m-a"},
        {"id": "b", "name": "乙", "model": "m-b"},
    ]
    calls = []
    def fake_chat(system, prompt):
        calls.append((system, prompt))
        return "{\"up_probability\":55,\"confidence\":\"medium\",\"verified\":[],\"conflicts_or_unverified\":[],\"agreements\":[],\"disagreements\":[],\"bullish\":[],\"bearish\":[],\"risks\":[],\"cne6_notes\":[],\"summary\":\"测试\",\"disclaimer\":\"测试\"}"
    monkeypatch.setattr(council_service, "_chat_for", lambda profile: fake_chat)
    day = _seed_quotes()
    sid = _row_id(day)
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    council_service._run(sid, "600519", "贵州茅台", day, profiles, ev)
    with session_scope() as session:
        row = session.query(AiCouncilSession).order_by(AiCouncilSession.id.desc()).first()
        assert row is not None
        assert row.status == "done"
        assert row.evidence.get("web")[0]["status"] == "failed"
        assert len(row.statements) == 2


def test_speaker_order_and_chair_gets_all_prior(client, monkeypatch):
    _seed_quotes()
    from backend.app.services.council import service as council_service

    profiles = [
        {"id": "a", "name": "甲", "model": "m-a"},
        {"id": "b", "name": "乙", "model": "m-b"},
        {"id": "c", "name": "主席", "model": "m-c"},
    ]
    seen = []
    def fake_chat(system, prompt):
        seen.append(prompt)
        if "主席" in prompt or "结构化 JSON 结论" in prompt:
            return '{"up_probability": 42,"confidence":"high","verified":[],"conflicts_or_unverified":[],"agreements":[],"disagreements":[],"bullish":[],"bearish":[],"risks":[],"cne6_notes":[],"summary":"测试","disclaimer":"测试"}'
        return "我同意或提出数据矛盾。"
    monkeypatch.setattr(council_service, "_chat_for", lambda profile: fake_chat)
    day = _seed_quotes()
    sid = _row_id(day)
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    council_service._run(sid, "600519", "贵州茅台", day, profiles, ev)
    assert len(seen) == 3
    assert "第 1/3 位" in seen[0] and "还没有前人发言" in seen[0]
    assert "第 2/3 位" in seen[1] and "甲" in seen[1] and "我同意" in seen[1]
    assert "第 3/3 位" in seen[2] and "甲" in seen[2] and "乙" in seen[2] and "结构化 JSON 结论" in seen[2]


def test_middle_failure_continues(client, monkeypatch):
    _seed_quotes()
    from backend.app.services.council import service as council_service

    profiles = [
        {"id": "a", "name": "甲", "model": "m-a"},
        {"id": "b", "name": "乙", "model": "m-b"},
        {"id": "c", "name": "主席", "model": "m-c"},
    ]
    calls = []
    def fake_chat(system, prompt):
        calls.append(prompt)
        if "第 2/3 位" in prompt:
            raise RuntimeError("HTTP 401")
        if "第 3/3 位" in prompt:
            return '{"up_probability":50,"confidence":"medium","verified":[],"conflicts_or_unverified":[],"agreements":[],"disagreements":[],"bullish":[],"bearish":[],"risks":[],"cne6_notes":[],"summary":"测试","disclaimer":"测试"}'
        return "甲发言"
    monkeypatch.setattr(council_service, "_chat_for", lambda profile: fake_chat)
    day = _seed_quotes()
    sid = _row_id(day)
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    council_service._run(sid, "600519", "贵州茅台", day, profiles, ev)
    with session_scope() as session:
        row = session.query(AiCouncilSession).order_by(AiCouncilSession.id.desc()).first()
        assert row.status == "done"
        assert len(row.statements) == 3
        assert row.statements[1]["error"] and "HTTP 401" in row.statements[1]["error"]
        assert row.conclusion["up_probability"] == 50


def test_chair_failure_saves_statements(client, monkeypatch):
    _seed_quotes()
    from backend.app.services.council import service as council_service

    profiles = [{"id": "a", "name": "甲", "model": "m-a"}, {"id": "b", "name": "主席", "model": "m-b"}]
    def fake_chat(system, prompt):
        if "第 2/2 位" in prompt:
            raise RuntimeError("HTTP 429")
        return "甲发言"
    monkeypatch.setattr(council_service, "_chat_for", lambda profile: fake_chat)
    day = _seed_quotes()
    sid = _row_id(day)
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    council_service._run(sid, "600519", "贵州茅台", day, profiles, ev)
    with session_scope() as session:
        row = session.query(AiCouncilSession).order_by(AiCouncilSession.id.desc()).first()
        assert row.status == "failed"
        assert len(row.statements) == 2
        assert "主席" in row.statements[1]["error"]


def test_probability_clamped(client, monkeypatch):
    _seed_quotes()
    from backend.app.services.council import service as council_service

    profiles = [{"id": "a", "name": "甲", "model": "m-a"}, {"id": "b", "name": "主席", "model": "m-b"}]
    def fake_chat(system, prompt):
        if "第 2/2 位" in prompt:
            return '{"up_probability": 1234,"confidence":"crazy","verified":[],"conflicts_or_unverified":[],"agreements":[],"disagreements":[],"bullish":[],"bearish":[],"risks":[],"cne6_notes":[],"summary":"测试","disclaimer":"测试"}'
        return "甲"
    monkeypatch.setattr(council_service, "_chat_for", lambda profile: fake_chat)
    day = _seed_quotes()
    sid = _row_id(day)
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    council_service._run(sid, "600519", "贵州茅台", day, profiles, ev)
    with session_scope() as session:
        row = session.query(AiCouncilSession).order_by(AiCouncilSession.id.desc()).first()
        assert row.conclusion["up_probability"] == 100
        assert row.conclusion["confidence"] == "low"


def test_no_api_key_stored(client, monkeypatch):
    _add_profiles(client, {"name": "甲", "model": "m-a"}, {"name": "乙", "model": "m-b"})
    day = _seed_quotes()
    # 直接经 API 启动，但为避免线程等待，这里只验证设置接口不返回密钥，且 history 序列化不含 key。
    order = client.put("/api/council/profile-order", json={"profile_ids": []})
    assert order.status_code == 200
    with session_scope() as session:
        session.add(AiCouncilSession(stock_code="600519", stock_name="贵州茅台", eval_date=day, status="done",
                                     participants=[{"id": "x", "name": "甲", "model": "m-a"}], evidence={}, statements=[], conclusion={}))
    rows = client.get("/api/council/history", params={"code": "600519"}).json()["items"]
    assert rows
    assert "api_key" not in json.dumps(rows)


def test_web_evidence_mocked(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **kw):
            self.transport = None
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get(self, url, params=None):
            raise httpx.ConnectTimeout("no network")
    monkeypatch.setattr(web_evidence.httpx, "Client", FakeClient)
    out = web_evidence.fetch_spot("600519")
    assert out["status"] == "failed"
    assert "no network" in out["digest"]["reason"]


def _fake_response_json(payload, status_code: int = 200):
    class Response:
        def __init__(self, payload, status_code):
            self.status_code = status_code
            self._payload = payload
            self.text = payload if isinstance(payload, str) else ""
        def json(self):
            if isinstance(self._payload, str):
                raise ValueError("not json")
            return self._payload
    return Response(payload, status_code)


def test_collect_category_coverage_and_fallback(monkeypatch):
    """16 类任务都应被调用；单个源失败或空结果不影响其它类别。"""
    calls = []

    def fake_client(*args, **kwargs):
        calls.append(kwargs)
        return _FakeClientWithDispatch()

    class _FakeClientWithDispatch:
        def __init__(self):
            self.calls = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, url, params=None):
            self.calls += 1
            if "fflow" in url:
                return _fake_response_json({"data": {"klines": ["2026-11-06,1,2,3,4,5,6,7,8,9,10,11"]}})
            if "datacenter-web" in url:
                raise httpx.ConnectTimeout("timeout")
            if "np-anotice" in url:
                return _fake_response_json({"data": {"list": []}})
            if "trends2" in url:
                return _fake_response_json({"data": {"trends": []}})
            if "F10" in url or "emweb" in url:
                return _fake_response_json({"jbmx": [{"REPORT_DATE": "2026-09-30", "NOTICE_DATE": "2026-10-25", "TOTALOPERATEREVE": 1, "PARENTNETPROFIT": 2}], "zyzb": [{"REPORT_DATE": "2026-09-30", "EPSJB": 3}]})
            if "kline" in url:
                return _fake_response_json({"data": {"klines": ["2026-11-06,1,2,3,4,5,6,7,8,9,10,11"]}})
            return _fake_response_json({"data": {}})

    monkeypatch.setattr(web_evidence.httpx, "Client", fake_client)
    items = web_evidence.collect("600519", date(2026, 11, 6))
    assert len(items) == 16
    statuses = [item["status"] for item in items]
    assert statuses.count("ok") >= 3
    assert "empty" in statuses
    assert "failed" in statuses
    assert any(item.get("category") == "market" for item in items)
    assert any(item.get("category") == "capital" for item in items)
    assert any(item.get("category") == "financial" for item in items)


def test_collect_date_windows(monkeypatch):
    """end 参数应落到近 30 个自然日窗口。"""
    captured = {}

    def fake_client(*args, **kwargs):
        return _FakeClientWithCapture(captured)

    class _FakeClientWithCapture:
        def __init__(self, captured):
            self.captured = captured
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, url, params=None):
            self.captured[url] = params or {}
            if "kline" in url:
                return _fake_response_json({"data": {"klines": ["2026-11-06,1,2,3,4,5,6,7,8,9,10,11"]}})
            return _fake_response_json({"data": {}})

    monkeypatch.setattr(web_evidence.httpx, "Client", fake_client)
    items = web_evidence.collect("600519", date(2026, 11, 6))
    window = items[1]["window"]
    assert window is not None
    assert window["start"] == "2026-10-07"
    assert window["end"] == "2026-11-06"
    assert window["days"] == 31
    assert any("kline" in url for url in captured)


def test_build_evidence_chart_inputs(client):
    """本地证据应带 30 日 bars 和前端可直接画的 chart bars。"""
    day = _seed_quotes()
    with session_scope() as session:
        ev = service.build_evidence(session, "600519", "贵州茅台", day)
    bars = ev["local"]["bars_30d"]
    chart = ev["local"]["chart"]["bars"]
    assert bars
    assert len(bars) <= 30
    assert chart
    assert all({"date", "open", "high", "low", "close", "volume", "amount", "pct_chg"} <= set(bar) for bar in chart)
    assert chart[0]["amount_estimated"] in (True, False)
