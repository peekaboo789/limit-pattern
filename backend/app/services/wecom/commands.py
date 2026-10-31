"""企业微信里发给应用的文字指令。只有白名单里的成员能用。"""

from __future__ import annotations

import re

from backend.app.core.database import session_scope
from backend.app.services.wecom import notify

HELP = ("可用指令：\n榜单 — 最新 AI 榜单前 10\n自选 — 自选股实时涨跌\n加自选 代码/名称 — 例：加自选 600519 或 加自选 茅台\n"
        "删自选 代码/名称\n采集 — 立即采集盘中行情，回复前 10（约半分钟）\n总结 — 最新每日总结\n帮助 — 显示本说明")


def parse(text: str) -> tuple[str, str]:
    t = re.sub(r"\s+", " ", str(text or "").strip())
    for cmd in ("加自选", "删自选", "榜单", "自选", "采集", "总结", "帮助"):
        if t.startswith(cmd):
            return cmd, t[len(cmd):].strip(" :：")
    if t.lower() in ("help", "?", "？"):
        return "帮助", ""
    return "", t


def allowed(user: str, admins: list[str]) -> bool:
    return bool(user) and user.lower() in {a.lower() for a in admins}


def _resolve(session, arg: str):
    from backend.app.services import watchlist_service as ws

    hits = ws.search(session, arg, limit=5)
    exact = [h for h in hits if h["code"] == ws.normalize_code(arg) or h.get("name") == arg]
    if exact:
        return exact[0], []
    if len(hits) == 1:
        return hits[0], []
    return None, hits


def run(cmd: str, arg: str) -> str:
    from backend.app.services import watchlist_service as ws

    if cmd in ("", "帮助"):
        return HELP if cmd else f"没看懂「{arg[:20]}」。\n{HELP}"
    if cmd == "榜单":
        from backend.app.services.ranking_service import get_ranking

        with session_scope() as s:
            board = get_ranking(s)
        return notify.ranking_text(board) if board else "还没有 AI 榜单"
    if cmd == "总结":
        from backend.app.models.tables import AiDailySummary

        with session_scope() as s:
            row = s.query(AiDailySummary).order_by(AiDailySummary.trade_date.desc()).first()
            payload = dict(row.payload) if row else None
        return notify.summary_text(payload) if payload else "还没有每日总结"
    if cmd == "自选":
        with session_scope() as s:
            snap = ws.snapshot(s)
        if not snap["items"]:
            return "自选股是空的。发「加自选 600519」添加"
        lines = [f"自选股 {snap['updated_at'][11:16]}" + (f"（{snap.get('price_label')}）" if snap.get("price_label") else "")]
        for r in snap["items"][:30]:
            lines.append(f"{r['name']}（{r['code']}） {r['price'] if r['price'] is not None else '—'} {notify._fmt_pct(r['pct'])}")
        return "\n".join(lines)
    if cmd in ("加自选", "删自选"):
        if not arg:
            return f"请带上代码或名称，例如：{cmd} 600519"
        with session_scope() as s:
            hit, options = _resolve(s, arg)
        if hit is None:
            if not options:
                return f"没找到「{arg}」"
            return "匹配到多只，请写完整代码：\n" + "\n".join(f"{o['code']} {o.get('name', '')}" for o in options)
        if cmd == "加自选":
            res = ws.add([{"code": hit["code"], "name": hit.get("name", "")}])
            return f"{'已在自选里' if hit['code'] in res['duplicates'] else '已加入自选'}：{hit.get('name', '')}（{hit['code']}）"
        before = {x["code"] for x in ws.load()}
        if hit["code"] not in before:
            return f"{hit.get('name', '')}（{hit['code']}）不在自选里"
        ws.remove([hit["code"]])
        return f"已删除自选：{hit.get('name', '')}（{hit['code']}）"
    if cmd == "采集":
        from backend.app.services import manual_capture

        try:
            result = manual_capture.capture()
        except Exception as exc:  # noqa: BLE001
            return f"采集没完成：{exc}"
        return notify.capture_text(result)
    return HELP
