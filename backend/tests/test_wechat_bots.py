"""个人微信渠道回归：严格返回校验、context_token 守卫、菜单卡片映射。"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.app.services.bots import router
from backend.app.services.bots.adapters import WeChatChannel
from backend.app.services.wechat import client as wechat_client
from backend.app.services.wechat import client
from backend.app.services.wecom import commands, menu


CFG = {
    "enabled": True,
    "base": "https://example.invalid",
    "token": "test-token",
    "username": "",
    "admins": [],
    "events": [],
}


class FakeTransport(httpx.BaseTransport):
    """按 URL 返回预设响应；sendmessage 只允许命中一次。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.sent = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendmessage"):
            self.sent.append(request)
            status = self.payload.get("http_status", 200)
            if "raw" in self.payload:
                return httpx.Response(status, text=self.payload["raw"])
            return httpx.Response(status, json=self.payload.get("json", {}))
        return httpx.Response(404, json={"ret": 0, "errcode": 0})


def _isolated(monkeypatch, tmp_path):
    """隔离会话文件、http 客户端与内存状态。"""
    monkeypatch.setattr(wechat_client, "_session_file", lambda: tmp_path / "wechat_session.json")
    monkeypatch.setattr(wechat_client, "_HTTP", {"client": None})
    with wechat_client._STATE_LOCK:
        wechat_client._STATE.clear()
        wechat_client._STATE.update({
            "context_token": None,
            "token": "test-token",
            "baseurl": "https://example.invalid",
            "bot_id": "bot",
            "ilink_user_id": "user-a",
            "username": "",
            "qrcode": None,
            "qrcode_img_content": None,
            "status": "confirmed",
            "admin": "user-a",
            "admin_bound_at": "2026-01-01T00:00:00+00:00",
            "context_tokens": {},
        })


def _set_ctx(monkeypatch, token):
    with wechat_client._STATE_LOCK:
        wechat_client._STATE["context_tokens"] = {"user-a": token}


def _fake_client(monkeypatch, payload):
    monkeypatch.setattr(wechat_client, "_http", lambda: httpx.Client(transport=FakeTransport(payload), timeout=5.0))
    return payload


def test_sendmessage_requires_context_token(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    with pytest.raises(wechat_client.WeChatError, match="暂时无法主动推送"):
        wechat_client.sendmessage(CFG, "hello")


def test_sendmessage_rejects_non_json(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    _set_ctx(monkeypatch, "ctx-1")
    _fake_client(monkeypatch, {"raw": "<html>not json</html>", "http_status": 200})
    with pytest.raises(wechat_client.WeChatError, match="非 JSON"):
        wechat_client.sendmessage(CFG, "hello")


def test_sendmessage_rejects_errcode(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    _set_ctx(monkeypatch, "ctx-1")
    _fake_client(monkeypatch, {"json": {"ret": 0, "errcode": -14, "errmsg": "session timeout"}})
    with pytest.raises(wechat_client.WeChatError, match="重新扫码"):
        wechat_client.sendmessage(CFG, "hello")


def test_sendmessage_stale_ret(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    _set_ctx(monkeypatch, "ctx-1")
    _fake_client(monkeypatch, {"json": {"ret": -14, "errcode": 0, "errmsg": "stale"}})
    with pytest.raises(wechat_client.WeChatError, match="重新扫码"):
        wechat_client.sendmessage(CFG, "hello")


def test_sendmessage_accepts_zero_both(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    _set_ctx(monkeypatch, "ctx-1")
    _fake_client(monkeypatch, {"json": {"ret": 0, "errcode": 0, "message_id": "m1"}})
    assert wechat_client.sendmessage(CFG, "hello")["message_id"] == "m1"


def test_channel_test_false_when_deliver_empty(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    ch = WeChatChannel()
    monkeypatch.setattr(ch, "deliver", lambda *a, **k: [])
    result = ch.test(CFG)
    assert result["ok"] is False
    assert "没有真正发出" in result["message"] or "暂时无法" in result["message"]


def test_channel_test_true_when_deliver_used(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    ch = WeChatChannel()
    monkeypatch.setattr(ch, "deliver", lambda *a, **k: ["wechat"])
    result = ch.test(CFG)
    assert result["ok"] is True
    assert result["channels"] == ["wechat"]


def test_menu_card_and_mapping():
    assert "一键采集+AI分析" in menu.wechat_menu_card()
    assert len(menu.wechat_menu_card().splitlines()) >= 18
    assert menu.wechat_menu_cmd("6") == "CAPTURE_AI"
    assert menu.wechat_menu_cmd("16") == ""
    assert menu.wechat_menu_cmd("") == ""


def test_wechat_menu_key_uses_shared_commands(monkeypatch):
    monkeypatch.setattr(commands, "run", lambda cmd, arg="", reply=None, user="": (cmd, "ran"))
    inbound = router.Inbound(channel="wechat", user="user-a", menu_key="CAPTURE_AI")
    assert router.run_command(WeChatChannel(), CFG, inbound) == ("采集分析", "ran")

