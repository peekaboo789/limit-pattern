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
        return list(cfg.get("admins") or [])

    def inbound(self, cfg: dict, text: str, user: str = "", menu_key: str = "") -> Inbound | None:
        return Inbound(channel=self.name, user=user, text=text)

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
        return [Inbound(channel=self.name, user=u["user"], text=u["text"]) for u in updates]

    def qr_status(self, cfg: dict | None = None) -> dict:
        cfg = cfg or self.config()
        return wechat_client.qr_status(cfg)

    def test(self, cfg: dict | None = None) -> dict:
        cfg = cfg or self.config()
        from backend.app import __version__
        from backend.app.core.clock import now

        stamp = now().strftime("%Y-%m-%d %H:%M:%S")
        used = self.deliver("✅ 涨停形态系统 测试消息", f"时间：{stamp}\n版本：v{__version__}\n收到这条说明个人微信推送配置正确。", cfg=cfg)
        return {"ok": True, "channels": used, "message": "已发送个人微信测试消息"}
