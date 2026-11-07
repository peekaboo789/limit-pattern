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


class _SingleResponseClient:
    def __init__(self, response):
        self.response = response
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def get(self, url, params=None):
        return self.response


def test_collect_category_coverage_and_fallback(monkeypatch):
    """17 类任务都应被调用，且包含新浪独立站点核对；单个源失败或空结果不影响其它类别。"""
    calls = []

    def fake_client(*args, **kwargs):
        calls.append(kwargs)
        return _FakeClientWithDispatch()

    class _FakeClientWithDispatch:
        def __init__(self):
            self.calls = 0
            self.headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, url, params=None):
            self.calls += 1
            if "hq.sinajs.cn" in url:
                class SinaResponse:
                    status_code = 200
                    text = 'var hq_str_sh600519="贵州茅台,1,2,3,4,5,0,0,8,9,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-11-06";'
                    def json(self):
                        raise ValueError("not json")
                return SinaResponse()
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
    assert len(items) == 17
    statuses = [item["status"] for item in items]
    assert statuses.count("ok") >= 3
    assert "empty" in statuses
    assert "failed" in statuses
    assert any(item.get("source") == "sina_spot" and item.get("status") == "ok" for item in items)
    assert any(item.get("category") == "market" for item in items)
    assert any(item.get("category") == "capital" for item in items)
    assert any(item.get("category") == "financial" for item in items)


def test_news_filters_out_of_window_and_future(monkeypatch):
    """资讯只保留 [评估日-30 天, 评估日]，未来和窗口外日期不得进证据。"""
    payload = 'jQuery({"result":{"cmsArticleWebOld":[' \
              '{"title":"窗口内","date":"2026-10-08","url":"https://x/1"},' \
              '{"title":"窗口外","date":"2026-09-01","url":"https://x/2"},' \
              '{"title":"未来","date":"2026-11-08","url":"https://x/3"}' \
              ']}})'

    class FakeResponse:
        status_code = 200
        text = payload
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(web_evidence.httpx, "Client", lambda *args, **kwargs: _SingleResponseClient(FakeResponse()))
    out = web_evidence.fetch_news("600519", date(2026, 11, 6))
    assert out["status"] == "ok"
    titles = [item["title"] for item in out["digest"]["items"]]
    assert titles == ["窗口内"]


def test_financial_main_picks_only_disclosed_before_end(monkeypatch):
    """财报只选评估日前已披露的最新报告，不能泄露未来披露。"""
    rows = [
        {"REPORT_DATE": "2026-09-30", "NOTICE_DATE": "2026-11-20", "TOTALOPERATEREVE": 999},
        {"REPORT_DATE": "2026-06-30", "NOTICE_DATE": "2026-08-25", "TOTALOPERATEREVE": 123},
    ]

    monkeypatch.setattr(web_evidence.httpx, "Client", lambda *args, **kwargs: _SingleResponseClient(_fake_response_json({"jbmx": rows})))
    out = web_evidence.fetch_financial_main("600519", date(2026, 11, 6))
    assert out["status"] == "ok"
    assert out["digest"]["latest"]["report_date"] == "2026-06-30"
    assert out["digest"]["latest"]["revenue"] == 123


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


def _seed_session(day, status="done", profiles=None, evidence=None, statements=None):
    from backend.app.models.tables import AiCouncilSession
    with session_scope() as session:
        row = AiCouncilSession(
            stock_code="600519", stock_name="贵州茅台", eval_date=day, status=status,
            participants=profiles or [{"id": "a", "name": "甲", "model": "m-a"}, {"id": "b", "name": "主席", "model": "m-b"}],
            evidence=evidence or {}, statements=statements or [], transcript=[], conclusion=None, error="",
        )
        session.add(row)
        session.commit()
        return row.id


def _fake_web():
    return [{"status": "failed", "category": "market", "source": "eastmoney_spot", "url": "https://x",
             "fetched_at": "now", "window": None, "digest": {"reason": "mock"}}]


def test_speaker_timeout_is_bounded(client, monkeypatch):
    """单个模型超时必须终止整场并持久化失败原因，不能无限转圈。"""
    from backend.app.services.council import service as council_service
    from backend.app.services.council import web_evidence as we_module
    from backend.app.core.ai_client import AiTimeout

    day = _seed_quotes()
    monkeypatch.setattr(we_module, "collect", lambda code, end=None: _fake_web())
    monkeypatch.setattr(we_module, "fetch_kline", lambda code, end=None, limit=35: {"status": "empty", "category": "market",
                     "source": "eastmoney_kline", "url": "https://x", "fetched_at": "now", "window": None, "digest": {"note": "mock"}})
    def fake_run_speaker(profile, system, prompt):
        raise AiTimeout("读取超时：连续 60 秒没有新数据")
    monkeypatch.setattr(council_service, "_run_speaker", fake_run_speaker)
    profiles = [{"id": "a", "name": "甲", "model": "m-a"}, {"id": "b", "name": "主席", "model": "m-b"}]
    sid = _seed_session(day, status="running")
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    council_service._run(sid, "600519", "贵州茅台", day, profiles, ev)
    with session_scope() as session:
        row = session.query(AiCouncilSession).filter(AiCouncilSession.id == sid).one()
        assert row.status == "failed"
        assert "超时" in row.error
        assert row.finished_at is not None
        assert row.transcript
        assert row.transcript[0]["error"]


def test_each_speaker_persisted_in_transcript(client, monkeypatch):
    """每个模型完成后立即写 DB；后续失败不丢前面的 transcript。"""
    from backend.app.services.council import service as council_service
    from backend.app.services.council import web_evidence as we_module

    day = _seed_quotes()
    monkeypatch.setattr(we_module, "collect", lambda code, end=None: _fake_web())
    monkeypatch.setattr(we_module, "fetch_kline", lambda code, end=None, limit=35: {"status": "empty", "category": "market",
                     "source": "eastmoney_kline", "url": "https://x", "fetched_at": "now", "window": None, "digest": {"note": "mock"}})
    def fake_run_speaker(profile, system, prompt):
        if profile["id"] == "b":
            raise RuntimeError("HTTP 429")
        return "甲的发言"
    monkeypatch.setattr(council_service, "_run_speaker", fake_run_speaker)
    profiles = [{"id": "a", "name": "甲", "model": "m-a"}, {"id": "b", "name": "主席", "model": "m-b"}]
    sid = _seed_session(day, status="running")
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    council_service._run(sid, "600519", "贵州茅台", day, profiles, ev)
    with session_scope() as session:
        row = session.query(AiCouncilSession).filter(AiCouncilSession.id == sid).one()
        assert len(row.transcript) == 2
        assert row.transcript[0]["content"] == "甲的发言"
        assert row.transcript[1]["error"]
        assert row.status == "failed"


def test_reconcile_stale_running_session(client):
    """进程重启后的遗留 running 会话要安全终止。"""
    day = _seed_quotes()
    sid = _seed_session(day, status="running")
    assert service.reconcile_stale_sessions() == 1
    with session_scope() as session:
        row = session.query(AiCouncilSession).filter(AiCouncilSession.id == sid).one()
        assert row.status == "failed"
        assert "重启" in row.error


def test_manual_speak_rejects_while_active(client, monkeypatch):
    """会话在自动开会/其它模型发言时，手动续轮必须拒绝并发。"""
    day = _seed_quotes()
    sid = _seed_session(day, status="running")
    service._mark_active(sid)
    try:
        with pytest.raises(RuntimeError) as exc:
            service.speak(sid, "a")
        assert "正在自动开会" in str(exc.value) or "已有模型发言" in str(exc.value)
    finally:
        service._unmark_active(sid)


def test_user_message_and_manual_speaker_enter_prompt(client, monkeypatch):
    """用户意见要进入后续模型 prompt，主席也应看到完整 transcript。"""
    from backend.app.services.council import service as council_service
    from backend.app.services.council import web_evidence as we_module

    day = _seed_quotes()
    monkeypatch.setattr(we_module, "collect", lambda code, end=None: _fake_web())
    monkeypatch.setattr(we_module, "fetch_kline", lambda code, end=None, limit=35: {"status": "empty", "category": "market",
                     "source": "eastmoney_kline", "url": "https://x", "fetched_at": "now", "window": None, "digest": {"note": "mock"}})
    seen = []
    def fake_run_speaker(profile, system, prompt):
        seen.append((profile["id"], prompt))
        if profile["id"] == "b":
            return '{"up_probability": 60,"confidence":"medium","verified":[],"conflicts_or_unverified":[],"agreements":[],"disagreements":[],"bullish":[],"bearish":[],"risks":[],"cne6_notes":[],"summary":"主席读到了","disclaimer":"测试"}'
        return "甲的发言"
    monkeypatch.setattr(council_service, "_run_speaker", fake_run_speaker)
    by_id = {"a": {"id": "a", "name": "甲", "model": "m-a", "api_key_set": True},
             "b": {"id": "b", "name": "主席", "model": "m-b", "api_key_set": True}}
    monkeypatch.setattr(council_service, "_profile_by_id", lambda pid: by_id[pid])
    profiles = [{"id": "a", "name": "甲", "model": "m-a"}, {"id": "b", "name": "主席", "model": "m-b"}]
    sid = _seed_session(day, status="running")
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    council_service._run(sid, "600519", "贵州茅台", day, profiles, ev)
    updated = council_service.add_user_message(sid, "用户补充：注意ST风险")
    assert any(item["kind"] == "user" and "ST" in item["content"] for item in updated["transcript"])
    updated = council_service.speak(sid, "a", round_no=2)
    assert updated["status"] == "done"
    assert len(seen) == 3
    assert "用户补充：注意ST风险" in seen[-1][1]
    # 主席应能读到完整 transcript：手动让主席总结，prompt 需包含用户意见与所有前序发言
    updated = council_service.speak(sid, "b", round_no=2)
    assert len(seen) == 4
    assert "用户补充：注意ST风险" in seen[-1][1]
    assert "甲的发言" in seen[-1][1]


def test_payload_uses_shanghai_timezone_and_duration(client):
    """时间输出统一为 Asia/Shanghai 标签，且给出 duration_seconds。"""
    day = _seed_quotes()
    sid = _seed_session(day, status="done", statements=[{"index": 0, "speaker": "甲", "model": "m-a", "content": "x", "error": ""}])
    payload = service.get(sid)
    assert payload["timezone"] == "Asia/Shanghai"
    assert payload["created_at"]
    assert not payload["created_at"].endswith("Z")
    assert "duration_seconds" in payload


def test_small_local_kline_supplemented_by_public(client, monkeypatch):
    """本地只有少量 K 线时，公开源补到可用范围，只含评估日及以前。"""
    from backend.app.services.council import service as council_service
    from backend.app.services.council import web_evidence as we_module

    day = _seed_quotes()
    monkeypatch.setattr(we_module, "collect", lambda code, end=None: _fake_web())
    public_bars = [
        {"date": "2026-06-08", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10, "amount": 100,
         "pct_chg": 1.0, "turnover_rate": 0.2, "is_limit_up": False, "is_limit_down": False},
        {"date": "2026-06-12", "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 10, "amount": 100,
         "pct_chg": 1.0, "turnover_rate": 0.2, "is_limit_up": False, "is_limit_down": False},
    ]
    monkeypatch.setattr(we_module, "fetch_kline", lambda code, end=None, limit=35: {
        "status": "ok", "category": "market", "source": "eastmoney_kline", "url": "https://x", "fetched_at": "now",
        "window": None, "digest": {"count": len(public_bars), "bars": public_bars}})
    with session_scope() as session:
        ev = council_service.build_evidence(session, "600519", "贵州茅台", day)
    web = _fake_web() + [we_module.fetch_kline("600519", day)]
    ev = council_service.finalize_evidence(ev, web, day)
    kline = ev["kline"]
    assert kline["local_count"] < 30
    assert kline["public_count"] == 2
    assert len(kline["merged_bars"]) >= len(ev["local"]["bars_30d"])
    assert all(bar["date"] <= day.isoformat() for bar in kline["merged_bars"])
    assert "本地近 30 个交易日只有" in kline["missing_reason"]
    assert kline["chart"]["bars"]


def test_scrub_removes_fake_api_key():
    """统一脱敏：假密钥不得进入错误/发言回显。"""
    assert "sk-***" in service._scrub("错误 sk-abcdefgh1234")
    assert service._scrub("Bearer abcdefgh1234567890").lower().startswith("bearer ***")


def test_council_models_endpoint_not_shadowed(client):
    """静态 /models 路由必须在动态 /{session_id} 之前注册。"""
    r = client.get("/api/council/models")
    assert r.status_code == 200
    assert "items" in r.json()


def test_user_message_endpoint_validates_and_scrubs(client, monkeypatch):
    """用户意见 API：空/超长校验；持久化后不回显密钥。"""
    day = _seed_quotes()
    sid = _seed_session(day, status="done")
    r = client.post(f"/api/council/{sid}/user-message", json={"text": ""})
    assert r.status_code == 409
    r = client.post(f"/api/council/{sid}/user-message", json={"text": "x" * 4001})
    assert r.status_code == 409
    r = client.post(f"/api/council/{sid}/user-message", json={"text": "意见包含 sk-abcdefgh1234 的假密钥"})
    assert r.status_code == 200
    payload = r.json()
    assert any(item["kind"] == "user" and "sk-***" in item["content"] for item in payload["transcript"])
    assert "sk-abcdefgh1234" not in json.dumps(payload)


def test_wall_clock_timeout_includes_internal_retries(monkeypatch):
    import threading
    release = threading.Event()
    monkeypatch.setattr(service, "COUNCIL_SPEAKER_TOTAL_SECONDS", 0.02)
    monkeypatch.setattr(service, "_chat_for", lambda profile: lambda *_: release.wait(1))
    try:
        with pytest.raises(TimeoutError, match="总时限"):
            service._run_speaker({"id": "test"}, "system", "prompt")
    finally:
        release.set()


def test_running_session_is_not_replaced(client, monkeypatch):
    monkeypatch.setitem(service._PROGRESS, "running", True)
    with pytest.raises(RuntimeError, match="已经有一场"):
        service.start("600759")


def test_configured_nonstandard_key_scrubbed_recursively(monkeypatch):
    key = "fake-private-provider-token-123456"
    monkeypatch.setattr(service.ai_profiles, "public", lambda: {"items": [{"id": "x"}]})
    monkeypatch.setattr(service.ai_profiles, "settings_for", lambda _: {"ai_api_key": key})
    result = service._sanitize({"transcript": [{"content": key}], "api_key": key, "error": key})
    assert key not in json.dumps(result)
    assert "api_key" not in result


def test_private_think_text_not_in_public_speech(monkeypatch):
    monkeypatch.setattr(service, "_chat_for", lambda _: lambda *_: "<think>private hidden reasoning</think>公开依据与疑点")
    assert service._run_speaker({"id": "fake"}, "system", "prompt") == "公开依据与疑点"


def test_phase_events_persist_and_survive_refresh(client):
    sid = _seed_session(_seed_quotes(), status="done")
    service._persist_fields(sid, progress={"phase": "verifying_web", "stage": "联网核对"})
    service._persist_fields(sid, progress={"phase": "speaking", "stage": "发言中", "current_model": "甲"})
    events = service.get(sid)["progress"]["events"]
    assert len(events) == 2
    assert events[0]["finished_at"]
    assert events[1]["model"] == "甲"


def test_group_chat_ui_contract():
    from pathlib import Path
    text = Path("frontend/src/views/CouncilView.vue").read_text()
    for feature in ("group-chat", "chat-avatar", "chat-bubble", "chat-composer", "chat-system", "发送意见", "请主持人总结", "followChat", "await loadSession(progress.value.session_id)"):
        assert feature in text
    assert 'position:sticky' in text
    assert '<summary>完整证据 JSON（默认折叠）</summary>' in text
    assert '不展示或伪造模型内部思考链' in text


def test_profile_order_route_not_shadowed(client):
    assert client.get("/api/council/profile-order").status_code == 200


def test_legacy_statements_preserved_when_user_adds_opinion(client):
    sid = _seed_session(_seed_quotes(), status="failed")
    service._persist_fields(sid, statements=[{"speaker": "旧模型", "model": "old", "content": "旧发言不能丢", "error": ""}])
    result = service.add_user_message(sid, "我的新意见")
    assert result["transcript"][0]["content"] == "旧发言不能丢"
    assert result["transcript"][1]["content"] == "我的新意见"


def test_existing_council_table_gets_chat_columns():
    from sqlalchemy import create_engine, inspect, text
    from backend.app.core.database import upgrade_existing_tables
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE ai_council_sessions (id INTEGER PRIMARY KEY, status TEXT)"))
        upgrade_existing_tables(engine)
        assert {"transcript", "progress"} <= {column["name"] for column in inspect(engine).get_columns("ai_council_sessions")}
    finally:
        engine.dispose()
