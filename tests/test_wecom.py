"""企业微信：加解密、签名、URL 验证、token 缓存刷新、错误码、报警去重与时段、指令与白名单、密钥脱敏、访问密码。

全部用假数据（官方文档里的示例 Token/EncodingAESKey/CorpID）和本地 Mock 服务，不连真实企业微信。"""

from __future__ import annotations

import json
import time
from datetime import datetime

import httpx
import pytest

from backend.app.services.wecom import client as wc
from backend.app.services.wecom.crypto import WeComCryptoError, WXBizMsgCrypt, signature

TOKEN = "QDG6eK"
AES = "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C"
CORP = "wx5823bf96d3bd56c7"
SECRET = "fake-secret-for-tests"


class MockWeCom:
    """模拟 qyapi.weixin.qq.com：gettoken + message/send，可编排错误码。"""

    def __init__(self):
        self.tokens = 0
        self.sent: list[dict] = []
        self.send_codes: list[int] = []
        self.token_code = 0
        self.hooks: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            if self.token_code:
                return httpx.Response(200, json={"errcode": self.token_code, "errmsg": "x"})
            assert request.url.params["corpsecret"] == SECRET
            self.tokens += 1
            return httpx.Response(200, json={"errcode": 0, "access_token": f"tok{self.tokens}", "expires_in": 7200})
        if request.url.path == "/cgi-bin/message/send":
            code = self.send_codes.pop(0) if self.send_codes else 0
            body = json.loads(request.content)
            body["_token"] = request.url.params["access_token"]
            if code == 0:
                self.sent.append(body)
            return httpx.Response(200, json={"errcode": code, "errmsg": "mock"})
        if request.url.path == "/cgi-bin/webhook/send":
            self.hooks.append(json.loads(request.content))
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        return httpx.Response(404)


@pytest.fixture()
def mock(monkeypatch, tmp_path):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(wc, "MIN_INTERVAL", 0)
    m = MockWeCom()
    wc.set_http_client(httpx.Client(transport=httpx.MockTransport(m.handler)))
    yield m
    wc.set_http_client(None)


_REAL_CONFIG = wc.config


def cfg(**kw):
    base = {"wecom_enabled": True, "wecom_corpid": CORP, "wecom_agentid": "1000002", "wecom_secret": SECRET,
            "wecom_proxy": "https://mock.local", "wecom_touser": "@all", "wecom_token": TOKEN, "wecom_aes_key": AES,
            "wecom_admins": "zhangsan, LiSi", "wecom_events": ["alert", "ranking", "summary", "sync_failed", "capture"]}
    base.update(kw)
    return _REAL_CONFIG(base)


def write_settings(client, **kw):
    body = {"wecom_enabled": True, "wecom_corpid": CORP, "wecom_agentid": "1000002", "wecom_secret": SECRET,
            "wecom_proxy": "https://mock.local", "wecom_token": TOKEN, "wecom_aes_key": AES, "wecom_admins": "zhangsan"}
    body.update(kw)
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- 加解密 ----------
def test_official_url_vector():
    c = WXBizMsgCrypt(TOKEN, AES, CORP)
    echo = "P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3zNdtp+4RPcs8TgAE7OaBO+FZXvnaqQ=="
    assert c.verify_url("5c45ff5e21c57e6ad56bac8758b79b1d9ac89fd3", "1409659589", "263014780", echo) == "1616140317555161061"
    with pytest.raises(WeComCryptoError) as e:
        c.verify_url("0" * 40, "1409659589", "263014780", echo)
    assert e.value.code == -40001


def test_round_trip_and_signature():
    c = WXBizMsgCrypt(TOKEN, AES, CORP)
    msg = "<xml><Content><![CDATA[榜单 测试 " + "x" * 100 + "]]></Content></xml>"
    out = c.encrypt_msg(msg, "nonce1", "1700000000")
    from backend.app.services.wecom.crypto import extract

    enc, sig = extract(out, "Encrypt"), extract(out, "MsgSignature")
    assert sig == signature(TOKEN, "1700000000", "nonce1", enc)
    assert c.decrypt_msg(out, sig, "1700000000", "nonce1") == msg
    with pytest.raises(WeComCryptoError):  # 别的企业的 receiveid
        WXBizMsgCrypt(TOKEN, AES, "wwother").decrypt(enc)
    with pytest.raises(WeComCryptoError):
        WXBizMsgCrypt(TOKEN, "short", CORP)


def test_xml_entities_rejected():
    from backend.app.services.wecom.crypto import extract

    with pytest.raises(WeComCryptoError):
        extract('<!DOCTYPE x [<!ENTITY a "b">]><xml><Encrypt>&a;</Encrypt></xml>', "Encrypt")


# ---------- 发送 ----------
def test_token_cached_and_refreshed(mock):
    c = cfg()
    wc.send(*wc.text("a"), cfg=c)
    wc.send(*wc.text("b"), cfg=c)
    assert mock.tokens == 1 and [m["_token"] for m in mock.sent] == ["tok1", "tok1"]
    mock.send_codes = [42001]  # 过期 → 刷新后重发
    wc.send(*wc.text("c"), cfg=c)
    assert mock.tokens == 2 and mock.sent[-1]["_token"] == "tok2" and mock.sent[-1]["text"]["content"] == "c"
    mock.send_codes = [40014]
    wc.send(*wc.markdown("**d**"), cfg=c)
    assert mock.tokens == 3 and mock.sent[-1]["msgtype"] == "markdown" and mock.sent[-1]["agentid"] == 1000002
    assert mock.sent[-1]["touser"] == "@all"


def test_error_mapping(mock):
    mock.send_codes = [60020]
    with pytest.raises(wc.WeComError) as e:
        wc.send(*wc.text("x"), cfg=cfg())
    assert e.value.code == 60020 and "可信IP" in str(e.value) and "代理" in str(e.value)
    mock.token_code = 40001
    wc.set_http_client(httpx.Client(transport=httpx.MockTransport(mock.handler)))
    with pytest.raises(wc.WeComError) as e:
        wc.send(*wc.text("x"), cfg=cfg())
    assert "Secret 不对" in str(e.value)
    assert "企业ID" in wc.explain(40013) and "AgentId" in wc.explain(40056) and "99999" in wc.explain(99999, "zz")


def test_network_error_is_plain_chinese(monkeypatch, tmp_path):
    monkeypatch.setattr(wc._time, "sleep", lambda s: None)

    def boom(request):
        raise httpx.ConnectError("refused")

    wc.set_http_client(httpx.Client(transport=httpx.MockTransport(boom)))
    try:
        with pytest.raises(wc.WeComError) as e:
            wc.access_token(cfg())
        assert "连不上" in str(e.value)
    finally:
        wc.set_http_client(None)


def test_webhook_channel(mock):
    c = cfg(wecom_secret="", wecom_webhook="https://mock.local/cgi-bin/webhook/send?key=fake")
    assert wc.send(*wc.markdown("hi"), cfg=c) == ["webhook"]
    assert mock.hooks[0]["markdown"]["content"] == "hi" and mock.tokens == 0


# ---------- 报警推送：时段 + 去重 ----------
def test_alert_dedupe_and_gating(mock, monkeypatch):
    from backend.app.services.wecom import notify

    monkeypatch.setattr(notify.client, "config", lambda raw=None: cfg())
    t = datetime(2026, 10, 30, 10, 0)
    items = [{"code": "600519", "name": "贵州茅台", "level": "warn", "pct": 3.4, "rank": 1}]
    assert notify.push_alerts(items, "ranking", t) == 1
    assert notify.push_alerts(items, "ranking", t) == 0  # 同档同日只推一次
    assert notify.push_alerts([dict(items[0], level="alert", pct=6.2)], "ranking", t) == 1  # 升档再推
    assert notify.push_alerts(items, "watchlist", t) == 1  # 自选和榜单分开计
    assert notify.push_alerts(items, "ranking", datetime(2026, 11, 2, 10, 0)) == 1  # 第二天重新计
    assert len(mock.sent) == 4 and "贵州茅台" in mock.sent[0]["markdown"]["content"]
    monkeypatch.setattr(notify.client, "config", lambda raw=None: cfg(wecom_events=["ranking"]))
    assert notify.push_alerts([dict(items[0], code="000001")], "ranking", t) == 0  # 没勾选报警事件


def test_loop_is_gated_outside_session(monkeypatch):
    from backend.app.services.wecom import loop

    called = []
    monkeypatch.setattr("backend.app.services.monitor_service.snapshot", lambda *a, **k: called.append(1) or {"items": []})
    assert loop.tick(datetime(2026, 10, 31, 10, 0)) == {"active": False, "pushed": 0}  # 周六
    assert loop.tick(datetime(2026, 10, 30, 12, 0))["active"] is False  # 午休
    assert called == []


# ---------- 指令 ----------
def test_command_parsing_and_whitelist():
    from backend.app.services.wecom import commands

    assert commands.parse("加自选 600519") == ("加自选", "600519")
    assert commands.parse("  删自选：茅台") == ("删自选", "茅台")
    assert commands.parse("榜单") == ("榜单", "")
    assert commands.parse("自选") == ("自选", "")
    assert commands.parse("help") == ("帮助", "")
    assert commands.parse("随便说说")[0] == ""
    assert commands.allowed("LISI", ["zhangsan", "LiSi"]) and not commands.allowed("wangwu", ["zhangsan"]) and not commands.allowed("", [])
    assert "加自选" in commands.run("帮助", "")


# ---------- 设置脱敏 ----------
def test_settings_masked(client):
    view = write_settings(client, wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fake")
    text = json.dumps(view, ensure_ascii=False)
    assert SECRET not in text and AES not in text and "fake" not in text
    assert view["wecom_secret_set"] and view["wecom_aes_key_set"] and view["wecom_token_set"] and view["wecom_webhook_set"]
    assert view["wecom_corpid"] == CORP and "wecom_secret" not in view and "wecom_aes_key" not in view
    view = client.put("/api/settings", json={"wecom_secret": "", "wecom_touser": "zhangsan"}).json()  # 空 = 保留
    assert view["wecom_secret_set"] and view["wecom_touser"] == "zhangsan"
    view = client.put("/api/settings", json={"wecom_webhook_clear": True}).json()
    assert view["wecom_webhook_set"] is False
    assert client.put("/api/settings", json={"wecom_aes_key": "too-short"}).status_code == 422
    assert client.put("/api/settings", json={"wecom_agentid": "abc"}).status_code == 422
    assert client.put("/api/settings", json={"wecom_proxy": "ftp://x"}).status_code == 422
    assert client.put("/api/settings", json={"wecom_events": ["bogus"]}).status_code == 422


# ---------- 端到端：测试按钮 + 回调 ----------
def test_test_button_end_to_end(client, mock):
    write_settings(client)
    r = client.post("/api/wecom/test").json()
    assert r["ok"] and r["errcode"] == 0 and "测试消息" in mock.sent[-1]["markdown"]["content"]
    mock.send_codes = [60020]
    r = client.post("/api/wecom/test").json()
    assert r["ok"] is False and r["errcode"] == 60020 and "可信IP" in r["message"]


def test_callback_verify_and_commands(client, mock, monkeypatch):
    write_settings(client)
    c = WXBizMsgCrypt(TOKEN, AES, CORP)
    echo = c.encrypt("echo-123")
    ts, nonce = str(int(time.time())), "n1"
    r = client.get("/api/wecom/callback", params={"msg_signature": signature(TOKEN, ts, nonce, echo), "timestamp": ts, "nonce": nonce, "echostr": echo})
    assert r.status_code == 200 and r.text == "echo-123"
    assert client.get("/api/wecom/callback", params={"msg_signature": "bad", "timestamp": ts, "nonce": nonce, "echostr": echo}).status_code == 403

    replies = []
    import backend.app.api.wecom_routes as routes

    monkeypatch.setattr(routes, "_reply_async", lambda cfg, info: replies.append(info))

    def post(user, content):
        xml = f"<xml><ToUserName><![CDATA[{CORP}]]></ToUserName><FromUserName><![CDATA[{user}]]></FromUserName><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[{content}]]></Content><AgentID>1000002</AgentID></xml>"
        out = c.encrypt_msg(xml, nonce, ts)
        from backend.app.services.wecom.crypto import extract

        return client.post("/api/wecom/callback", params={"msg_signature": extract(out, "MsgSignature"), "timestamp": ts, "nonce": nonce}, content=out)

    assert post("zhangsan", "加自选 600519").text == "success"
    assert replies[-1]["cmd"] == "加自选" and replies[-1]["arg"] == "600519"
    post("wangwu", "榜单")
    assert replies[-1]["cmd"] is None and "白名单" in replies[-1]["reply"]


def test_commands_run_watchlist(client, mock):
    from backend.app.services import watchlist_service as ws
    from backend.app.services.wecom import commands

    ws.add([{"code": "600519", "name": "贵州茅台"}])
    assert "已在自选里" in commands.run("加自选", "600519")
    assert "已删除自选" in commands.run("删自选", "600519")
    assert "不在自选里" in commands.run("删自选", "600519")
    assert "还没有 AI 榜单" in commands.run("榜单", "")
    assert "还没有每日总结" in commands.run("总结", "")


def test_callback_disabled_returns_404(client):
    assert client.get("/api/wecom/callback").status_code == 404


# ---------- 访问密码 ----------
def test_auth_env_password(client, monkeypatch):
    monkeypatch.setenv("LP_PASSWORD", "pa55word")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/auth/status").json() == {"required": True, "logged_in": False, "headless": False}
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "pa55word"}).status_code == 200
    assert client.get("/api/settings").status_code == 200
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/settings").status_code == 401


def test_auth_settings_password_headless_only(client, monkeypatch):
    monkeypatch.delenv("LP_PASSWORD", raising=False)
    assert client.put("/api/settings", json={"access_password": "abc"}).status_code == 422
    view = client.put("/api/settings", json={"access_password": "secret-pw"}).json()
    assert view["access_password_set"] and "secret-pw" not in json.dumps(view)
    from backend.app.core.config import read_settings_file

    assert read_settings_file()["access_password"].startswith("pbkdf2$")
    assert client.get("/api/settings").status_code == 200  # 桌面（非 headless）不挡
    monkeypatch.setenv("LP_HEADLESS", "1")
    assert client.get("/api/settings").status_code == 401
    assert client.post("/api/auth/login", json={"password": "secret-pw"}).status_code == 200
    assert client.get("/api/settings").status_code == 200
    # 改密码后旧会话作废
    client.put("/api/settings", json={"access_password": "another-pw"})
    assert client.get("/api/settings").status_code == 401
