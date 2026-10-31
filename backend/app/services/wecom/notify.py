"""把系统事件推到企业微信。所有函数都"尽力而为"：没开启/没配置/没勾选事件时直接返回，出错只记日志不抛。

v0.5.1：内容一律是 (标题, 纯文本正文)，由 client.deliver 按设置的格式发送——默认文本，个人微信（微信插件）也能显示。"""

from __future__ import annotations

import threading
from datetime import datetime

from backend.app.core.clock import now
from backend.app.core.logging import get_logger
from backend.app.services.wecom import client

logger = get_logger("limit_pattern.wecom")
LEVEL_TEXT = {"warn": "≥3% 预警", "alert": "≥6% 报警"}
LEVEL_ICON = {"warn": "🟠", "alert": "🔴"}


def wants(event: str, cfg: dict | None = None) -> dict | None:
    cfg = cfg or client.config()
    if not cfg["enabled"] or not client.ready(cfg) or event not in cfg["events"]:
        return None
    return cfg


def _fire(event: str, build, background: bool = True) -> None:
    cfg = wants(event)
    if cfg is None:
        return

    def run():
        try:
            msg = build()
            if msg:
                client.deliver(*msg, cfg=cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("企业微信推送（%s）失败：%s", event, exc)

    if background:
        threading.Thread(target=run, name=f"wecom-{event}", daemon=True).start()
    else:
        run()


def fmt_pct(pct) -> str:
    return "—" if pct is None else f"{float(pct):+.2f}%"


_fmt_pct = fmt_pct


def stock(name: str, code: str) -> str:
    return f"{name or ''}({code})"


def push_alerts(items: list[dict], source: str, moment: datetime | None = None, background: bool = False) -> int:
    """items: [{code, name, level, pct, rank?}]；source: "ranking" / "watchlist"。每天每只每档一次。"""
    cfg = wants("alert")
    if cfg is None or not items:
        return 0
    moment = moment or now()
    fresh = [x for x in items if x.get("level") in LEVEL_TEXT and client.once(f"{source}:{x['code']}:{x['level']}", moment.date())]
    if not fresh:
        return 0
    label = "AI 榜单" if source == "ranking" else "自选股"
    lines = []
    for x in fresh[:20]:
        rank = f"#{x['rank']} " if x.get("rank") else ""
        lines.append(f"{LEVEL_ICON[x['level']]} {rank}{stock(x.get('name'), x['code'])} {fmt_pct(x.get('pct'))} {LEVEL_TEXT[x['level']]}")
    _fire("alert", lambda: (f"🚨 {label}盘中提醒 {moment.strftime('%H:%M')}", "\n".join(lines)), background)
    return len(fresh)


def ranking_text(payload: dict, top: int = 10) -> tuple[str, str]:
    kind = {"external": "外部AI导入", "manual": "手动生成", "schedule": "定时生成"}.get(payload.get("trigger"), payload.get("trigger") or "")
    lines = []
    for it in (payload.get("items") or [])[:top]:
        score = it.get("composite_score")
        lines.append(f"{it.get('rank'):>2}. {stock(it.get('name'), it['code'])} 综合 {'—' if score is None else round(float(score), 1)}")
    return f"📊 AI 榜单 {payload.get('date', '')}（{kind}，共{len(payload.get('items') or [])}只）", "\n".join(lines) or "（空）"


def ranking_ready(payload: dict) -> None:
    if wants("ranking") and client.once(f"ranking:{payload.get('date')}:{payload.get('run_at')}"):
        _fire("ranking", lambda: ranking_text(payload))


def summary_text(payload: dict) -> tuple[str, str]:
    lines = [payload.get("headline") or ""] + [f"• {p}" for p in (payload.get("key_points") or [])[:6]]
    return f"📝 每日总结 {payload.get('date', '')}", "\n".join(x for x in lines if x)


def summary_done(payload: dict) -> None:
    if wants("summary") and client.once(f"summary:{payload.get('date')}:{payload.get('generated_at')}"):
        _fire("summary", lambda: summary_text(payload))


def sync_failed(message: str) -> None:
    _fire("sync_failed", lambda: ("⚠️ 日线同步失败", message[:300]))


def capture_text(result: dict, top: int = 10) -> tuple[str, str]:
    items = (result.get("result") or result).get("items") or []
    ranked = sorted([i for i in items if i.get("composite_score") is not None], key=lambda i: -float(i["composite_score"]))
    lines = [f"{n:>2}. {stock(it.get('name'), it['code'])} 综合 {round(float(it['composite_score']), 1)} 相似 {it.get('max_similarity') or '—'}"
             for n, it in enumerate(ranked[:top], 1)]
    return f"📸 盘中采集 {result.get('label') or result.get('date', '')} 前{top}", "\n".join(lines) or "没有带信号的候选"


def capture_done(result: dict) -> None:
    _fire("capture", lambda: capture_text(result))
