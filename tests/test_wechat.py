"""个人微信 iLink Bot / ClawBot 通道：扫码、状态、长轮询、纯文本收发、设置脱敏、渠道无关命令与推送。"""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from backend.app.services.wechat import client as wx
from backend.app.services.wecom import notify


class MockILink:
    """模拟 iLink Bot / ClawBot：qrcode、qrcode_status、getupdates、sendmessage。"""

    def __init__(self, token="tok-test", user="wxid_alice"):
        self.qr_code = "data:image/png;base64,Zm9v"
        self.token = token
        self.user = user
        self.sent: list[dict] = []
        self.updates: list[dict] = []
        self.context = "ctx-1"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path.endswith("/get_bot_qrcode"):
            return httpx.Response(200, json={"qrcode": self.qr_code, "expires_in": 120})
        if path.endswith("/get_qrcode_status"):
            return httpx.Response(200, json={"code": 0, "token": self.token, "context_token": self.context})
        if path.endswith("/getupdates"):
            return httpx.Response(200, json={"data": {"messages": self.updates}, "context_token": "ctx-2"})
        if path.endswith("/sendmessage"):
            self.sent.append(body)
            return httpx.Response(200, json={"errcode": 0})
        return httpx.Response(404, json={})


@pytest.fixture()
def mock(monkeypatch, tmp_path):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    wx._STATE.update(token=None, context_token=None, qrcode=None)
    m = MockILink()
    wx.set_http_client(httpx.Client(transport=httpx.MockTransport(m.handler)))
    yield m
    wx.set_http_client(None)
    wx._STATE.update(token=None, context_token=None, qrcode=None)


def cfg(**kw):
    base = {"enabled": True, "base": "https://mock.local", "token": "", "username": "", "admins": ["wxid_alice"],
            "events": ["alert", "ranking", "summary", "sync_failed", "capture"]}
    base.update(kw)
    return base


def test_qr_login_and_session_persist(mock, tmp_path):
    c = cfg()
    assert not wx.ready(c)
    qr = wx.get_qrcode(c)
    assert qr["qrcode"].startswith("data:image")
    st = wx.qr_status(c)
    assert st["logged_in"] and wx.ready(c)
    assert (tmp_path / "wechat_session.json").exists()


def test_send_plain_text_with_token(mock, tmp_path):
    c = cfg(token="tok-static")
    used = wx.deliver("标题", "正文", cfg=c)
    assert used == ["wechat"] and mock.sent[-1]["content"] == "标题\n正文"
    # 公共设置不回显 token，只给 token_set；wx.config() 内部可含 token
    assert "token" not in __import__("backend.app.core.config", fromlist=["wechat_public"]).wechat_public({"wechat_token": "tok-static"})
    assert wx.config(raw={"wechat_token": "tok-static"})["token"] == "tok-static"


def test_getupdates_and_dispatch_command(mock, monkeypatch, tmp_path):
    c = cfg()
    wx.get_qrcode(c)
    wx.qr_status(c)
    mock.updates = [{"from_user": "wxid_alice", "text": "榜单"}]
    from backend.app.services.bots import router
    from backend.app.services.wecom import commands

    monkeypatch.setattr(router, "enabled_channels", lambda raw=None: [router.channel("wechat")])
    monkeypatch.setattr(router.channel("wechat"), "config", lambda raw=None: cfg())
    monkeypatch.setattr(router.channel("wechat"), "enabled", lambda raw=None: True)
    monkeypatch.setattr(router.channel("wechat"), "ready", lambda c: True)
    handled = []
    monkeypatch.setattr(commands, "run", lambda cmd, arg="", reply=None, user="": handled.append((cmd, user)) or ("📊", "结果"))
    count = router.dispatch_polled()
    assert count == 1 and handled == [("榜单", "wxid_alice")]
    assert mock.sent[-1]["content"].startswith("📊")


def test_push_alert_uses_bot_router_and_wechat_only(mock, monkeypatch):
    from backend.app.services.bots import router

    monkeypatch.setattr(router, "enabled_channels", lambda raw=None: [router.channel("wechat")])
    monkeypatch.setattr(router.channel("wechat"), "config", lambda raw=None: cfg())
    monkeypatch.setattr(router.channel("wechat"), "enabled", lambda raw=None: True)
    monkeypatch.setattr(router.channel("wechat"), "ready", lambda c: True)
    wx._STATE.update(token="tok-test")
    items = [{"code": "600519", "name": "贵州茅台", "level": "warn", "pct": 3.2, "rank": 1}]
    assert notify.push_alerts(items, "ranking", datetime(2026, 10, 30, 10, 0), background=False) == 1
    assert "贵州茅台" in mock.sent[-1]["content"]


def test_settings_masked_and_update(client):
    view = client.put("/api/settings", json={
        "wechat_enabled": True,
        "wechat_api_base": "https://ilinkai.weixin.qq.com",
        "wechat_token": "super-secret-token",
        "wechat_admins": "wxid_alice,wxid_bob",
        "wechat_events": ["alert", "summary"],
    }).json()
    assert view["wechat_enabled"] is True
    assert view["wechat_token_set"] is True
    assert "super-secret-token" not in json.dumps(view, ensure_ascii=False)
    assert "wechat_token" not in view
    assert view["wechat_admins"] == "wxid_alice,wxid_bob"
    assert client.put("/api/settings", json={"wechat_events": ["bogus"]}).status_code == 422
    assert client.put("/api/settings", json={"wechat_api_base": "ftp://x"}).status_code == 422
    cleared = client.put("/api/settings", json={"wechat_token_clear": True}).json()
    assert cleared["wechat_token_set"] is False


def test_wechat_endpoints(mock, client, monkeypatch):
    from backend.app.services.bots import router

    client.put("/api/settings", json={"wechat_enabled": True, "wechat_api_base": "https://mock.local", "wechat_admins": "wxid_alice"})
    qr = client.get("/api/wechat/qrcode")
    assert qr.status_code == 200 and qr.json()["qrcode"].startswith("data:image")
    st = client.get("/api/wechat/status").json()
    assert st["logged_in"] and st["ready"]
    r = client.post("/api/wechat/test")
    assert r.status_code == 200 and r.json()["ok"]
    assert mock.sent[-1]["content"].startswith("✅")
