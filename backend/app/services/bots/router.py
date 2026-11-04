"""渠道无关的命令 / 推送路由层。

企业微信、个人微信等渠道只做适配器：实现统一的 BotChannel 接口，然后注册到
BOT_CHANNELS。业务代码（notify.push_* / commands.run / 后台轮询）一律只调用这里，
不再直接 import 某一渠道的 client / notify。

设计约定：
- 文本一律是 (title, body) 纯文本，渠道适配器负责裁剪和发送（个人微信只发纯文本）。
- 去重按“渠道 + key + 日期”落盘，各渠道天然隔离，但同一事件同一渠道只发一次。
- 一个事件可以同时推给多个已启用渠道；命令只由消息来源渠道回给该用户。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Protocol

from backend.app.core.logging import get_logger

logger = get_logger("limit_pattern.bots")


@dataclass(frozen=True)
class Inbound:
    """渠道适配器解析出的入站消息。"""

    channel: str
    user: str
    text: str = ""
    menu_key: str = ""
    pending_pick: bool = False


class BotChannel(Protocol):
    name: str

    def enabled(self, raw: dict | None = None) -> bool: ...

    def ready(self, cfg: dict) -> bool: ...

    def config(self, raw: dict | None = None) -> dict: ...

    def deliver(self, title: str, body: str, *, touser: str | None = None, cfg: dict | None = None) -> list[str]: ...

    def admins(self, cfg: dict) -> list[str]: ...

    def inbound(self, cfg: dict, text: str, user: str = "", menu_key: str = "") -> Inbound | None: ...

    def reply(self, cfg: dict, user: str, title: str, body: str) -> None: ...

    def poll_once(self) -> list[Inbound]: ...

    def qr_status(self, cfg: dict | None = None) -> dict: ...

    def test(self, cfg: dict | None = None) -> dict: ...


_BOT_CHANNELS: dict[str, BotChannel] = {}


def register(channel: BotChannel) -> None:
    _BOT_CHANNELS[channel.name] = channel


def channel(name: str) -> BotChannel:
    return _BOT_CHANNELS[name]


def channels(raw: dict | None = None) -> list[BotChannel]:
    return list(_BOT_CHANNELS.values())


def enabled_channels(raw: dict | None = None) -> list[BotChannel]:
    return [c for c in channels(raw) if c.enabled(raw)]


def admins_for(ch: BotChannel, cfg: dict) -> list[str]:
    try:
        return ch.admins(cfg)
    except Exception:  # noqa: BLE001
        logger.exception("读取 %s 白名单失败", ch.name)
        return []


def deliver_to(
    name: str,
    title: str,
    body: str,
    *,
    touser: str | None = None,
    raw: dict | None = None,
    cfg: dict | None = None,
) -> list[str]:
    ch = channel(name)
    if cfg is None:
        cfg = ch.config(raw)
    if not ch.ready(cfg):
        return []
    return ch.deliver(title, body, touser=touser, cfg=cfg)


def broadcast(
    title: str,
    body: str,
    *,
    raw: dict | None = None,
    only: Iterable[str] | None = None,
) -> list[str]:
    """给所有已启用且配置完整的渠道推送；only 可限制渠道名。失败只记日志。"""
    used: list[str] = []
    allowed = set(only) if only is not None else None
    for ch in enabled_channels(raw):
        if allowed is not None and ch.name not in allowed:
            continue
        try:
            cfg = ch.config(raw)
            if ch.ready(cfg):
                used += ch.deliver(title, body, cfg=cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 推送失败：%s", ch.name, exc)
    return used


def poll_all() -> list[tuple[str, Inbound]]:
    """拉取所有已启用渠道的入站消息（企业微信走回调，这里只有微信长轮询）。"""
    from backend.app.core.logging import get_logger

    out: list[tuple[str, Inbound]] = []
    for ch in enabled_channels():
        try:
            for inbound in ch.poll_once():
                out.append((ch.name, inbound))
        except Exception as exc:  # noqa: BLE001
            get_logger("limit_pattern.bots").warning("%s 拉取消息失败：%s", ch.name, exc)
    return out


def dispatch_polled() -> int:
    """把 poll_all 拿到的消息交给命令层执行，返回处理条数。"""
    handled = 0
    for name, inbound in poll_all():
        ch = channel(name)
        cfg = ch.config()
        if not ch.ready(cfg):
            continue
        try:
            msg = run_command(ch, cfg, inbound)
            if msg:
                ch.reply(cfg, inbound.user, *msg)
                handled += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 处理入站消息失败：%s", name, exc)
    return handled


def run_command(
    ch: BotChannel,
    cfg: dict,
    inbound: Inbound,
) -> tuple[str, str] | None:
    """渠道无关命令执行。返回 (title, body) 给入站渠道回复；None 表示后台任务自会回复。"""
    from backend.app.services.wecom import commands

    if not commands.allowed(inbound.user, admins_for(ch, cfg)):
        return "🔒 没有权限", "你不在本系统的指令白名单里，请联系管理员添加。"
    if inbound.pending_pick:
        return commands.pick(inbound.user, inbound.text)
    if inbound.menu_key:
        cmd = commands.MENU.get(inbound.menu_key, "")
        arg = ""
        if not cmd:
            return None
    else:
        if inbound.text.isdigit() and commands.pending_pick(inbound.user):
            return commands.pick(inbound.user, inbound.text)
        cmd, arg = commands.parse(inbound.text)
        if not cmd:
            return "🤔 没看懂", f"「{inbound.text[:20]}」不是指令。\n{commands.HELP_BODY}"

    def reply(title: str, body: str) -> None:
        try:
            ch.reply(cfg, inbound.user, title, body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 回复失败：%s", ch.name, exc)

    return commands.run(cmd, arg, reply=reply, user=inbound.user)


def wants_event(ch: BotChannel, raw: dict | None, event: str) -> dict | None:
    cfg = ch.config(raw)
    if not ch.enabled(raw) or not ch.ready(cfg) or event not in cfg.get("events", []):
        return None
    return cfg


def wants_event_for(event: str, raw: dict | None = None) -> dict | None:
    """任一已启用且勾选了该事件的渠道配置就绪时返回 True。"""
    for ch in enabled_channels(raw):
        if wants_event(ch, raw, event) is not None:
            return {"channel": ch.name}
    return None


def push_event(event: str, title: str, body: str, *, background: bool = True) -> None:
    """给所有想接收该事件的渠道推送。失败只记日志。"""
    if background:
        import threading

        threading.Thread(target=lambda: push_event(event, title, body, background=False), name=f"bot-{event}", daemon=True).start()
        return
    for ch in enabled_channels():
        cfg = wants_event(ch, None, event)
        if cfg is None:
            continue
        try:
            ch.deliver(title, body, cfg=cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 推送（%s）失败：%s", ch.name, event, exc)


# ---------- 去重：渠道隔离，日期分片 ----------
_DEDUPE_LOCK = __import__("threading").Lock()


def once(key: str, day: date | None = None) -> bool:
    from backend.app.services.wecom.client import once as _wecom_once

    return _wecom_once(key, day)
