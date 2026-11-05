"""各渠道适配器：把企业微信 / 个人微信封装成统一 BotChannel。"""

from __future__ import annotations

from backend.app.services.bots.router import BotChannel, Inbound
from backend.app.services.wecom import client as wecom_client
from backend.app.services.wechat import client as wechat_client


class WeComChannel:
    name = "wecom"

    def enabled(self, raw: dict | None = None) -> bool:
        return (raw or wecom_client.read_settings_file()).get("wecom_enabled") is True

    def ready(self, cfg: dict) -> bool:
        return wecom_client.ready(cfg)

    def config(self, raw: dict | None = None) -> dict:
        return wecom_client.config(raw)

    def deliver(self, title: str, body: str, *, touser: str | None = None, cfg: dict | None = None) -> list[str]:
        return wecom_client.deliver(title, body, touser=touser, cfg=cfg)

    def admins(self, cfg: dict) -> list[str]:
        return list(cfg.get("admins") or [])

    def inbound(self, cfg: dict, text: str, user: str = "", menu_key: str = "") -> Inbound | None:
        return Inbound(channel=self.name, user=user, text=text, menu_key=menu_key)

    def reply(self, cfg: dict, user: str, title: str, body: str) -> None:
        wecom_client.deliver(title, body, touser=user, cfg=cfg)

    def poll_once(self) -> list[Inbound]:
        return []

    def qr_status(self, cfg: dict | None = None) -> dict:
        return {}

    def test(self, cfg: dict | None = None) -> dict:
        cfg = cfg or self.config()
        from backend.app import __version__
        from backend.app.core.clock import now

        stamp = now().strftime("%Y-%m-%d %H:%M:%S")
        used = self.deliver("✅ 涨停形态系统 测试消息", f"时间：{stamp}\n版本：v{__version__}\n收到这条说明企业微信推送配置正确。", cfg=cfg)
        return {"ok": True, "channels": used, "message": "已发送企业微信测试消息"}


class WeChatChannel:
    name = "wechat"

    def enabled(self, raw: dict | None = None) -> bool:
        return (raw or wechat_client.read_settings_file()).get("wechat_enabled") is True

    def ready(self, cfg: dict) -> bool:
        return wechat_client.ready(cfg)

    def config(self, raw: dict | None = None) -> dict:
        return wechat_client.config(raw)

    def deliver(self, title: str, body: str, *, touser: str | None = None, cfg: dict | None = None) -> list[str]:
        return wechat_client.deliver(title, body, touser=touser, cfg=cfg)

    def admins(self, cfg: dict) -> list[str]:
        return [wechat_client.current_admin(cfg)] if wechat_client.current_admin(cfg) else []

    def inbound(self, cfg: dict, text: str, user: str = "", menu_key: str = "") -> Inbound | None:
        return Inbound(channel=self.name, user=user, text=text, menu_key=menu_key)

    def reply(self, cfg: dict, user: str, title: str, body: str) -> None:
        wechat_client.deliver(title, body, touser=user, cfg=cfg)

    def poll_once(self) -> list[Inbound]:
        cfg = self.config()
        if not (self.enabled() and self.ready(cfg)):
            return []
        try:
            updates = wechat_client.getupdates(cfg, long_poll=True)
        except Exception as exc:  # noqa: BLE001
            from backend.app.core.logging import get_logger

            get_logger("limit_pattern.wechat").warning("微信长轮询失败：%s", exc)
            return []
        out = []
        for u in updates:
            user = u["user"]
            if not user:
                continue
            # 第一个发消息的人自动绑定为管理员；其他人消息忽略。
            bound_now = wechat_client.bind_first_sender(cfg, user)
            is_admin = bound_now or wechat_client.current_admin(cfg) == user
            if is_admin:
                out.append(Inbound(channel=self.name, user=user, text=u["text"], first_bind=bound_now))
        return out

    def qr_status(self, cfg: dict | None = None) -> dict:
        cfg = cfg or self.config()
        return wechat_client.qr_status(cfg)

    def test(self, cfg: dict | None = None) -> dict:
        cfg = cfg or self.config()
        if not wechat_client.current_admin_bound(cfg):
            return {"ok": False, "channels": [], "message": "还没绑定管理员：请先用自己的微信给机器人发任意一条消息"}
        from backend.app import __version__
        from backend.app.core.clock import now

        stamp = now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            used = self.deliver("✅ 涨停形态系统 测试消息", f"时间：{stamp}\n版本：v{__version__}\n收到这条说明个人微信推送配置正确。", cfg=cfg)
        except wechat_client.WeChatError as exc:
            return {"ok": False, "channels": [], "message": str(exc), "errcode": exc.code}
        if not used:
            return {"ok": False, "channels": [], "message": "测试消息没有真正发出：还没收到你发给机器人的消息，暂时无法主动推送——请先用你自己的微信给机器人发任意一条消息，等系统收到后再点发送测试消息。"}
        return {"ok": True, "channels": used, "message": "已发送个人微信测试消息"}
