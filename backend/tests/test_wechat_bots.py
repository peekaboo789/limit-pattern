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


class _FakeWeComChannel:
    name = "wecom"

    def __init__(self):
        self.replies = []

    def config(self, raw=None):
        return {"admins": ["wecom-user"]}

    def ready(self, cfg):
        return True

    def poll_once(self):
        return []

    def reply(self, cfg, user, title, body):
        self.replies.append((title, body))


class _FakeWeChatChannel:
    name = "wechat"

    def __init__(self, enabled=True, ready=True, admin="wx-admin", deliver_error=None, context_token="ctx-1"):
        self.enabled_value = enabled
        self.ready_value = ready
        self.admin_value = admin
        self.deliver_error = deliver_error
        self.context_token = context_token
        self.delivered = []

    def enabled(self, raw=None):
        return self.enabled_value

    def ready(self, cfg):
        return self.ready_value

    def config(self, raw=None):
        return {"enabled": self.enabled_value}

    def admins(self, cfg):
        return [self.admin_value] if self.admin_value else []

    def mirror_recipient(self, cfg=None):
        if not self.enabled_value:
            return None, "wechat_disabled"
        if not self.ready_value:
            return None, "wechat_not_ready"
        if not self.admin_value:
            return None, "wechat_admin_not_bound"
        if not self.context_token:
            return None, "wechat_no_context_token"
        return self.admin_value, ""

    def deliver(self, title, body, *, touser=None, cfg=None):
        if self.deliver_error:
            raise self.deliver_error
        self.delivered.append((touser, title, body))
        return ["wechat"]

    def reply(self, cfg, user, title, body):
        if self.deliver_error:
            raise self.deliver_error
        self.delivered.append((user, title, body))
        return ["wechat"]


def _install_fake_wechat(monkeypatch, fake):
    monkeypatch.setattr(router, "channel", lambda name: fake)


def test_command_reply_mirrors_wecom_to_wechat(monkeypatch):
    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel()
    _install_fake_wechat(monkeypatch, wechat)

    router.command_reply(wecom, {"corpid": "x", "agentid": "y", "secret": "z"}, "wecom-user", "标题", "正文")

    assert wecom.replies == [("标题", "正文")]
    assert wechat.delivered == [("wx-admin", "标题", "正文")]


def test_command_reply_skips_wechat_when_disabled(monkeypatch):
    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel(enabled=False)
    _install_fake_wechat(monkeypatch, wechat)

    router.command_reply(wecom, {}, "wecom-user", "标题", "正文")

    assert wecom.replies == [("标题", "正文")]
    assert wechat.delivered == []


def test_command_reply_skips_wechat_when_not_ready(monkeypatch):
    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel(ready=False)
    _install_fake_wechat(monkeypatch, wechat)

    router.command_reply(wecom, {}, "wecom-user", "标题", "正文")

    assert wecom.replies == [("标题", "正文")]
    assert wechat.delivered == []


def test_command_reply_skips_wechat_when_no_admin(monkeypatch):
    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel(admin="")
    _install_fake_wechat(monkeypatch, wechat)

    router.command_reply(wecom, {}, "wecom-user", "标题", "正文")

    assert wecom.replies == [("标题", "正文")]
    assert wechat.delivered == []


def test_command_reply_skips_wechat_when_whitelist_only(monkeypatch):
    """管理员只来自设置白名单、并未真实绑定时不镜像，但企业微信仍成功。"""
    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel(admin="whitelist-only", context_token="ctx-1")
    wechat.mirror_recipient = lambda cfg=None: (None, "wechat_admin_not_bound")
    _install_fake_wechat(monkeypatch, wechat)

    router.command_reply(wecom, {}, "wecom-user", "标题", "正文")

    assert wecom.replies == [("标题", "正文")]
    assert wechat.delivered == []


def test_command_reply_skips_wechat_when_no_context_token(monkeypatch):
    """已绑定管理员但缺 context_token 时不镜像，企业微信仍成功。"""
    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel(admin="wx-admin", context_token="")
    _install_fake_wechat(monkeypatch, wechat)

    router.command_reply(wecom, {}, "wecom-user", "标题", "正文")

    assert wecom.replies == [("标题", "正文")]
    assert wechat.delivered == []


def test_command_reply_uses_bound_admin_not_wecom_user(monkeypatch):
    """镜像 touser 是个人微信绑定的管理员，不是企业微信 userid。"""
    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel(admin="bound-admin")
    _install_fake_wechat(monkeypatch, wechat)

    router.command_reply(wecom, {}, "wecom-user", "标题", "正文")

    assert wecom.replies == [("标题", "正文")]
    assert wechat.delivered == [("bound-admin", "标题", "正文")]


def test_mirror_recipient_reports_skip_reason(monkeypatch):
    """辅助函数可直接断言四个跳过原因。"""
    assert router.mirror_recipient(_FakeWeChatChannel(enabled=False), {}) == (None, "wechat_disabled")
    assert router.mirror_recipient(_FakeWeChatChannel(ready=False), {}) == (None, "wechat_not_ready")
    assert router.mirror_recipient(_FakeWeChatChannel(admin=""), {}) == (None, "wechat_admin_not_bound")
    assert router.mirror_recipient(_FakeWeChatChannel(context_token=""), {}) == (None, "wechat_no_context_token")
    assert router.mirror_recipient(_FakeWeChatChannel(admin="bound-admin"), {}) == ("bound-admin", "")


def test_command_reply_wecom_success_when_mirror_fails(monkeypatch):
    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel(deliver_error=RuntimeError("send failed"))
    _install_fake_wechat(monkeypatch, wechat)

    router.command_reply(wecom, {}, "wecom-user", "标题", "正文")

    assert wecom.replies == [("标题", "正文")]
    assert wechat.delivered == []


def test_command_reply_wechat_origin_does_not_mirror_to_wecom(monkeypatch):
    origin = _FakeWeChatChannel(admin="origin-admin")
    target = _FakeWeComChannel()
    # router.channel() only matters for mirror target; origin wechat should not trigger it.
    monkeypatch.setattr(router, "channel", lambda name: target)

    router.command_reply(origin, {}, "wx-user", "标题", "正文")

    assert origin.delivered == [("wx-user", "标题", "正文")]
    assert target.replies == []


def test_run_command_sync_reply_is_mirrored(monkeypatch):
    import backend.app.services.wecom.commands as commands

    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel()
    _install_fake_wechat(monkeypatch, wechat)
    monkeypatch.setattr(commands, "allowed", lambda user, admins: True)
    monkeypatch.setattr(commands, "parse", lambda text: ("帮助", ""))
    monkeypatch.setattr(commands, "run", lambda cmd, arg="", reply=None, user="": ("📖 可用指令", "帮助正文"))

    inbound = router.Inbound(channel="wecom", user="wecom-user", text="帮助")
    result = router.run_command(wecom, {"admins": ["wecom-user"]}, inbound)

    assert result == ("📖 可用指令", "帮助正文")
    # 同步返回值由 dispatch_polled 用 command_reply 发送；run_command 本身不发送，
    # 所以这里单独验证 dispatch_polled 路径。


def test_dispatch_polled_wecom_sync_reply_mirrors_to_wechat(monkeypatch):
    import backend.app.services.wecom.commands as commands

    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel()

    monkeypatch.setattr(router, "enabled_channels", lambda raw=None: [wecom])
    monkeypatch.setattr(router, "channel", lambda name: wecom if name == "wecom" else wechat)
    monkeypatch.setattr(wecom, "poll_once", lambda: [router.Inbound(channel="wecom", user="wecom-user", text="帮助")])
    monkeypatch.setattr(commands, "allowed", lambda user, admins: True)
    monkeypatch.setattr(commands, "parse", lambda text: ("帮助", ""))
    monkeypatch.setattr(commands, "run", lambda cmd, arg="", reply=None, user="": ("📖 可用指令", "帮助正文"))

    assert router.dispatch_polled() == 1
    assert wecom.replies == [("📖 可用指令", "帮助正文")]
    assert wechat.delivered == [("wx-admin", "📖 可用指令", "帮助正文")]


def test_run_command_background_replies_are_mirrored(monkeypatch):
    import backend.app.services.wecom.commands as commands

    wecom = _FakeWeComChannel()
    wechat = _FakeWeChatChannel()
    _install_fake_wechat(monkeypatch, wechat)
    monkeypatch.setattr(commands, "allowed", lambda user, admins: True)
    monkeypatch.setattr(commands, "parse", lambda text: ("采集分析", ""))

    replies = [("⏳ 已开始", "正在开始"), ("✅ 完成", "最终结果")]

    def fake_run(cmd, arg="", reply=None, user=""):
        if reply is None:
            return replies[0]
        for item in replies:
            reply(*item)
        return None

    monkeypatch.setattr(commands, "run", fake_run)

    inbound = router.Inbound(channel="wecom", user="wecom-user", text="采集分析")
    assert router.run_command(wecom, {"admins": ["wecom-user"]}, inbound) is None

    assert wecom.replies == replies
    assert wechat.delivered == [("wx-admin", *item) for item in replies]


def test_push_event_not_double_mirrored(monkeypatch):
    fake = _FakeWeChatChannel()
    monkeypatch.setattr(router, "enabled_channels", lambda raw=None: [fake])
    monkeypatch.setattr(router, "channel", lambda name: fake)

    fake.config = lambda raw=None: {"enabled": True, "events": ["alert"]}
    fake.delivered.clear()
    router.push_event("alert", "标题", "正文", background=False)

    assert fake.delivered == [(None, "标题", "正文")]
    # push_event 直接 deliver；不调用 command_reply，因此只有一条。

