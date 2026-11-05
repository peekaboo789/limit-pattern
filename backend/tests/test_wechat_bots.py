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
    assert len(menu.wechat_menu_card().splitlines()) >= 20
    assert menu.wechat_menu_cmd("1") == "RANK_TODAY"
    assert menu.wechat_menu_cmd("6") == "CAPTURE_AI"
    assert menu.wechat_menu_cmd("11") == "WL_QUOTE"
    assert menu.wechat_menu_cmd("15") == "HELP"
    assert menu.wechat_menu_cmd("0") == ""
    assert menu.wechat_menu_cmd("16") == ""
    assert menu.wechat_menu_cmd("") == ""


@pytest.mark.parametrize("text,key", [
    ("工具1", "CAPTURE_AI"),
    ("工具 1", "CAPTURE_AI"),
    ("工具5", "SUMMARY"),
    ("榜单2", "RANK_LIVE"),
    ("自选3", "WL_EXPORT"),
])
def test_wechat_group_cmd_valid(text, key):
    assert menu.wechat_group_cmd(text) == key


@pytest.mark.parametrize("text", ["工具9", "工具0", "榜单0", "自选6", "乱写", "", "工具x", "AI1"])
def test_wechat_group_cmd_invalid(text):
    assert menu.wechat_group_cmd(text) == ""


def test_card_numbers_match_flat_mapping():
    import re

    flat = menu.flat_items()
    lines = menu.wechat_menu_card().splitlines()
    for n, (_group, name, key) in enumerate(flat, 1):
        expected = f"{n}. {name}"
        assert expected in lines
        assert menu.wechat_menu_cmd(str(n)) == key


def test_wechat_menu_key_uses_shared_commands(monkeypatch):
    monkeypatch.setattr(commands, "run", lambda cmd, arg="", reply=None, user="": (cmd, "ran"))
    inbound = router.Inbound(channel="wechat", user="user-a", menu_key="CAPTURE_AI")
    assert router.run_command(WeChatChannel(), CFG, inbound) == ("采集分析", "ran")


class _PickWeChatChannel(WeChatChannel):
    """用于 dispatch_polled 集成测试：记录 run_command 收到的入站并返回固定回复。"""

    def __init__(self):
        self.seen: list[router.Inbound] = []
        self.replies: list[tuple[str, str]] = []

    def poll_once(self):
        return [router.Inbound(channel=self.name, user="user-a", text="2", pending_pick=False)]

    def config(self, raw=None):
        return CFG

    def ready(self, cfg):
        return True

    def admins(self, cfg):
        return ["user-a"]

    def reply(self, cfg, user, title, body):
        self.replies.append((title, body))


def test_dispatch_polled_pending_pick_digit_skips_menu(monkeypatch):
    import backend.app.services.wecom.commands as commands

    ch = _PickWeChatChannel()
    monkeypatch.setattr(router, "enabled_channels", lambda raw=None: [ch])
    monkeypatch.setattr(router, "channel", lambda name: ch)
    monkeypatch.setattr(commands, "pending_pick", lambda user: True)
    monkeypatch.setattr(commands, "pick", lambda user, text: ("✅ 已切换AI", "当前模型：模型 B"))
    assert router.dispatch_polled() == 1
    assert ch.replies == [("✅ 已切换AI", "当前模型：模型 B")]
    assert all(not inbound.menu_key for inbound in ch.seen)

