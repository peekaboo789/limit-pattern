"""个人微信 iLink Bot / ClawBot 通道：官方协议扫码、状态、长轮询、纯文本收发、首个发送者绑定管理员。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from backend.app.services.wechat import client as wx
from backend.app.services.wecom import notify


class MockILink:
    """模拟官方 iLink Bot 协议。"""

    def __init__(self, token="tok-test", user="wxid_alice"):
        self.qr_code = "qr-123"
        self.qr_img = "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=qr-123&bot_type=3"
        self.token = token
        self.user = user
        self.sent: list[dict] = []
        self.updates: list[dict] = []
        self.get_updates_buf = "buf-1"
        self.baseurl = "https://mock.local"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path.endswith("/ilink/bot/get_bot_qrcode"):
            assert request.method == "POST"
            return httpx.Response(200, json={"qrcode": self.qr_code, "qrcode_img_content": self.qr_img, "ret": 0})
        if path.endswith("/ilink/bot/get_qrcode_status"):
            assert request.method == "GET"
            return httpx.Response(200, json={"status": "confirmed", "bot_token": self.token,
                                             "ilink_bot_id": "bot-1", "ilink_user_id": self.user,
                                             "baseurl": self.baseurl})
        if path.endswith("/ilink/bot/getupdates"):
            assert request.method == "POST"
            return httpx.Response(200, json={"ret": 0, "msgs": self.updates, "get_updates_buf": "buf-2"})
        if path.endswith("/ilink/bot/sendmessage"):
            self.sent.append(body)
            return httpx.Response(200, json={"ret": 0, "message_id": "msg-id-1"})
        return httpx.Response(404, json={})


@pytest.fixture()
def mock(monkeypatch, tmp_path):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    wx._STATE.update(token=None, baseurl=None, bot_id=None, ilink_user_id=None, username=None,
                     get_updates_buf=None, qrcode=None, qrcode_img_content=None, status=None,
                     admin=None, admin_bound_at=None, context_tokens={})
    m = MockILink()
    wx.set_http_client(httpx.Client(transport=httpx.MockTransport(m.handler)))
    yield m
    wx.set_http_client(None)
    wx._STATE.update(token=None, baseurl=None, bot_id=None, ilink_user_id=None, username=None,
                     get_updates_buf=None, qrcode=None, qrcode_img_content=None, status=None,
                     admin=None, admin_bound_at=None, context_tokens={})


def cfg(**kw):
    base = {"enabled": True, "base": "https://mock.local", "token": "", "username": "",
            "admins": [], "events": ["alert", "ranking", "summary", "sync_failed", "capture"]}
    base.update(kw)
    return base


def text_msg(user, text, context_token="ctx-1"):
    return {
        "from_user_id": user,
        "to_user_id": "bot-1",
        "message_type": 1,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
        "context_token": context_token,
    }


def test_qr_login_and_session_persist(mock, tmp_path):
    c = cfg()
    assert not wx.ready(c)
    qr = wx.get_qrcode(c)
    assert qr["qrcode"].startswith("http")
    assert qr["qrcode_img_content"].startswith("http")
    assert qr["qrcode_id"] == "qr-123"
    st = wx.qr_status(c)
    assert st["logged_in"] and wx.ready(c)
    assert (tmp_path / "wechat_session.json").exists()
    saved = json.loads((tmp_path / "wechat_session.json").read_text())
    assert saved["token"] == "tok-test" and saved["ilink_user_id"] == "wxid_alice"


def test_send_plain_text_with_token(mock, tmp_path):
    c = cfg(token="tok-static", admins=["wxid_alice"])
    wx._STATE.update(admin="wxid_alice", context_tokens={"wxid_alice": "ctx-1"})
    used = wx.deliver("标题", "正文", cfg=c)
    assert used == ["wechat"]
    msg = mock.sent[-1]["msg"]
    assert msg["to_user_id"] == "wxid_alice"
    assert msg["item_list"][0]["text_item"]["text"] == "标题\n正文"
    assert mock.sent[-1]["base_info"]["channel_version"]


def test_getupdates_and_dispatch_command(mock, monkeypatch, tmp_path):
    c = cfg()
    wx.get_qrcode(c)
    wx.qr_status(c)
    mock.updates = [text_msg("wxid_alice", "榜单")]
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
    sent = mock.sent[-1]["msg"]
    assert sent["to_user_id"] == "wxid_alice"
    assert sent["context_token"] == "ctx-1"


def test_first_sender_binds_and_others_ignored(mock, monkeypatch):
    c = cfg()
    wx.get_qrcode(c)
    wx.qr_status(c)
    # 两个并发用户同时首发：只有一个绑定。
    mock.updates = [text_msg("wxid_alice", "榜单"), text_msg("wxid_bob", "榜单")]
    from backend.app.services.bots import router
    from backend.app.services.wecom import commands

    monkeypatch.setattr(router, "enabled_channels", lambda raw=None: [router.channel("wechat")])
    monkeypatch.setattr(router.channel("wechat"), "config", lambda raw=None: cfg())
    monkeypatch.setattr(router.channel("wechat"), "enabled", lambda raw=None: True)
    monkeypatch.setattr(router.channel("wechat"), "ready", lambda c: True)
    handled = []
    monkeypatch.setattr(commands, "run", lambda cmd, arg="", reply=None, user="": handled.append((cmd, user)) or ("📊", "结果"))
    assert router.dispatch_polled() == 1
    assert wx.current_admin(c) in ("wxid_alice", "wxid_bob")
    # 第二人下一轮被忽略
    mock.updates = [text_msg("wxid_bob", "榜单")]
    assert router.dispatch_polled() == 0


def test_unbind_then_rebind(mock, tmp_path):
    c = cfg()
    wx.get_qrcode(c)
    wx.qr_status(c)
    assert wx.bind_first_sender(c, "wxid_alice") is True
    assert wx.bind_first_sender(c, "wxid_alice") is True
    assert wx.current_admin(c) == "wxid_alice"
    wx.unbind_admin(c)
    assert wx.current_admin(c) == ""
    assert wx.bind_first_sender(c, "wxid_bob") is True
    assert wx.current_admin(c) == "wxid_bob"


def test_push_alert_uses_bot_router_and_wechat_only(mock, monkeypatch):
    from backend.app.services.bots import router

    monkeypatch.setattr(router, "enabled_channels", lambda raw=None: [router.channel("wechat")])
    monkeypatch.setattr(router.channel("wechat"), "config", lambda raw=None: cfg(admins=["wxid_alice"]))
    monkeypatch.setattr(router.channel("wechat"), "enabled", lambda raw=None: True)
    monkeypatch.setattr(router.channel("wechat"), "ready", lambda c: True)
    wx._STATE.update(token="tok-test", admin="wxid_alice", context_tokens={"wxid_alice": "ctx-1"})
    items = [{"code": "600519", "name": "贵州茅台", "level": "warn", "pct": 3.2, "rank": 1}]
    assert notify.push_alerts(items, "ranking", datetime(2026, 10, 30, 10, 0), background=False) == 1
    assert "贵州茅台" in mock.sent[-1]["msg"]["item_list"][0]["text_item"]["text"]


def test_settings_masked_and_update(client):
    view = client.put("/api/settings", json={
        "wechat_enabled": True,
        "wechat_api_base": "https://ilinkai.weixin.qq.com",
        "wechat_token": "super-secret-token",
        "wechat_events": ["alert", "summary"],
    }).json()
    assert view["wechat_enabled"] is True
    assert view["wechat_token_set"] is True
    assert "super-secret-token" not in json.dumps(view, ensure_ascii=False)
    assert "wechat_token" not in view
    assert "wechat_admins" not in view
    assert client.put("/api/settings", json={"wechat_events": ["bogus"]}).status_code == 422
    assert client.put("/api/settings", json={"wechat_api_base": "ftp://x"}).status_code == 422
    cleared = client.put("/api/settings", json={"wechat_token_clear": True}).json()
    assert cleared["wechat_token_set"] is False


def test_wechat_endpoints(mock, client, monkeypatch):
    from backend.app.services.bots import router

    client.put("/api/settings", json={"wechat_enabled": True, "wechat_api_base": "https://mock.local"})
    qr = client.get("/api/wechat/qrcode")
    assert qr.status_code == 200 and qr.json()["qrcode"].startswith("http")
    st = client.get("/api/wechat/status").json()
    assert st["logged_in"] and st["ready"]
    assert st["admin"]["bound"] is False
    r = client.post("/api/wechat/test")
    assert r.status_code == 200 and r.json()["ok"] is False
    unb = client.post("/api/wechat/unbind")
    assert unb.status_code == 200 and unb.json()["ok"]
