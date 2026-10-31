"""把系统事件推到企业微信。所有函数都"尽力而为"：没开启/没配置/没勾选事件时直接返回，出错只记日志不抛。"""

from __future__ import annotations

import threading
from datetime import datetime

from backend.app.core.clock import now
from backend.app.core.logging import get_logger
from backend.app.services.wecom import client

logger = get_logger("limit_pattern.wecom")
LEVEL_TEXT = {"warn": "≥3% 预警", "alert": "≥6% 报警"}


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
                client.send(*msg, cfg=cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("企业微信推送（%s）失败：%s", event, exc)

    if background:
        threading.Thread(target=run, name=f"wecom-{event}", daemon=True).start()
    else:
        run()


def _fmt_pct(pct) -> str:
    return "—" if pct is None else f"{float(pct):+.2f}%"


# ---------- 盘中报警（榜单 + 自选），每天每只每档一次 ----------
def push_alerts(items: list[dict], source: str, moment: datetime | None = None, background: bool = False) -> int:
    """items: [{code, name, level, pct, rank?}]；source: "ranking" / "watchlist"。返回新推送的条数。"""
    cfg = wants("alert")
    if cfg is None or not items:
        return 0
    moment = moment or now()
    fresh = [x for x in items if x.get("level") in LEVEL_TEXT and client.once(f"{source}:{x['code']}:{x['level']}", moment.date())]
    if not fresh:
        return 0
    label = "AI 榜单" if source == "ranking" else "自选股"
    lines = [f"**{label}盘中提醒** {moment.strftime('%H:%M')}"]
    for x in fresh[:20]:
        color = "warning" if x["level"] == "alert" else "info"
        rank = f"#{x['rank']} " if x.get("rank") else ""
        lines.append(f"> {rank}{x.get('name') or ''}（{x['code']}） <font color=\"{color}\">{_fmt_pct(x.get('pct'))}</font> {LEVEL_TEXT[x['level']]}")
    _fire("alert", lambda: client.markdown("\n".join(lines)), background)
    return len(fresh)


# ---------- 榜单 / 总结 / 同步失败 / 采集 ----------
def ranking_text(payload: dict, top: int = 10) -> str:
    kind = {"external": "外部 AI 导入", "manual": "手动生成", "schedule": "定时生成"}.get(payload.get("trigger"), payload.get("trigger") or "")
    lines = [f"**AI 榜单 {payload.get('date', '')}**（{kind}，{len(payload.get('items') or [])} 只）"]
    for it in (payload.get("items") or [])[:top]:
        score = it.get("composite_score")
        lines.append(f"{it.get('rank')}. {it.get('name') or ''}（{it['code']}） 综合 {'—' if score is None else round(float(score), 1)}")
    return "\n".join(lines)


def ranking_ready(payload: dict) -> None:
    if client.once(f"ranking:{payload.get('date')}:{payload.get('run_at')}") if wants("ranking") else False:
        _fire("ranking", lambda: client.markdown(ranking_text(payload)))


def summary_text(payload: dict) -> str:
    lines = [f"**每日总结 {payload.get('date', '')}**", payload.get("headline") or ""]
    for point in (payload.get("key_points") or [])[:6]:
        lines.append(f"> {point}")
    return "\n".join(x for x in lines if x)


def summary_done(payload: dict) -> None:
    if wants("summary") and client.once(f"summary:{payload.get('date')}:{payload.get('generated_at')}"):
        _fire("summary", lambda: client.markdown(summary_text(payload)))


def sync_failed(message: str) -> None:
    _fire("sync_failed", lambda: client.text(f"【涨停形态系统】日线同步失败：{message[:300]}"))


def capture_text(result: dict, top: int = 10) -> str:
    lines = [f"**盘中采集 {result.get('label') or result.get('date', '')}** 前 {top}"]
    items = (result.get("result") or result).get("items") or []
    ranked = sorted([i for i in items if i.get("composite_score") is not None], key=lambda i: -float(i["composite_score"]))
    for n, it in enumerate(ranked[:top], 1):
        lines.append(f"{n}. {it.get('name') or ''}（{it['code']}） 综合 {round(float(it['composite_score']), 1)} 相似 {it.get('max_similarity') or '—'}")
    if len(lines) == 1:
        lines.append("没有带信号的候选")
    return "\n".join(lines)


def capture_done(result: dict) -> None:
    _fire("capture", lambda: client.markdown(capture_text(result)))
