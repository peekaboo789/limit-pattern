"""企业微信里的文字指令和底部菜单点击。只有白名单里的成员能用。

run() 返回 (标题, 正文)；耗时操作（采集 / 同步 / 重新生成 / 相似扫描）先回「已开始…」，做完再用 reply() 推结果。"""

from __future__ import annotations

import re
import threading
import time as _time
from typing import Callable

from backend.app.core.database import session_scope
from backend.app.services.wecom import notify

Reply = Callable[[str, str], None]

HELP_BODY = "\n".join([
    "📊 榜单：榜单 / 实时监控 / 相似前10 / 观察池 / 重新生成",
    "⭐ 自选：自选 / 异动 / 加自选 代码或名称 / 删自选 代码或名称 / 导出自选",
    "🛠 工具：采集 / 同步 / 总结 / 状态 / 检查更新",
    "也可以直接点对话框底部的菜单。",
])

# 菜单 key → 指令
MENU = {
    "RANK_TODAY": "榜单", "RANK_LIVE": "实时监控", "SIM_TOP": "相似前10", "NEXT_POOL": "观察池", "RANK_REGEN": "重新生成",
    "WL_QUOTE": "自选", "WL_MOVERS": "异动", "WL_ADD_HELP": "添加说明", "WL_DEL_HELP": "删除说明", "WL_EXPORT": "导出自选",
    "CAPTURE": "采集", "SYNC": "同步", "SUMMARY": "总结", "STATUS": "状态", "UPDATE": "检查更新",
}
# 文字指令（含别名），长的放前面先匹配
ALIASES = [
    ("加自选", "加自选"), ("添加自选", "加自选"), ("删自选", "删自选"), ("删除自选", "删自选"),
    ("导出自选", "导出自选"), ("自选异动", "异动"), ("异动", "异动"), ("自选行情", "自选"), ("自选", "自选"),
    ("今日AI榜单", "榜单"), ("AI榜单", "榜单"), ("重新生成榜单", "重新生成"), ("重新生成", "重新生成"), ("榜单", "榜单"),
    ("实时监控", "实时监控"), ("监控", "实时监控"), ("相似前10", "相似前10"), ("相似", "相似前10"),
    ("次日观察池", "观察池"), ("观察池", "观察池"),
    ("采集当前行情", "采集"), ("采集", "采集"), ("同步行情", "同步"), ("同步", "同步"),
    ("今日总结", "总结"), ("总结", "总结"), ("系统状态", "状态"), ("状态", "状态"),
    ("检查更新", "检查更新"), ("更新", "检查更新"), ("帮助", "帮助"), ("菜单", "帮助"),
]


def parse(text: str) -> tuple[str, str]:
    t = re.sub(r"\s+", " ", str(text or "").strip())
    for word, cmd in ALIASES:
        if t.upper().startswith(word.upper()):
            return cmd, t[len(word):].strip(" :：")
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


def _later(reply: Reply | None, job: Callable[[], tuple[str, str]], started: str) -> tuple[str, str]:
    """后台做 job，做完 reply；先返回「已开始」。没有 reply（测试）时同步执行。"""
    if reply is None:
        return job()

    def run():
        try:
            reply(*job())
        except Exception as exc:  # noqa: BLE001
            reply("❌ 没完成", str(exc)[:300])

    threading.Thread(target=run, name="wecom-job", daemon=True).start()
    return "⏳ 已开始", started


def _scan_lines(items: list[dict], key: str = "max_similarity") -> str:
    return "\n".join(f"{n:>2}. {notify.stock(i.get('name'), i['code'])} {key == 'prediction_score' and '观察分' or '相似'} {i.get(key)}"
                     for n, i in enumerate(items[:10], 1)) or "（空）"


def run(cmd: str, arg: str = "", reply: Reply | None = None) -> tuple[str, str]:
    from backend.app.services import watchlist_service as ws

    if cmd == "帮助":
        return "📖 可用指令", HELP_BODY
    if cmd == "":
        return "🤔 没看懂", f"「{arg[:20]}」不是指令。\n{HELP_BODY}"
    if cmd == "榜单":
        from backend.app.services.ranking_service import get_ranking

        with session_scope() as s:
            board = get_ranking(s)
        return notify.ranking_text(board) if board else ("📊 AI 榜单", "还没有 AI 榜单")
    if cmd == "实时监控":
        from backend.app.services import monitor_service

        with session_scope() as s:
            snap = monitor_service.snapshot(s)
        if not snap.get("items"):
            return "📈 榜单实时监控", snap.get("error") or "没有数据"
        lines = [f"{notify.LEVEL_ICON.get(i.get('raw_level') or i['level'], '▫️')} {i.get('rank'):>2}. {notify.stock(i['name'], i['code'])} "
                 f"{i['price'] if i['price'] is not None else '—'} {notify.fmt_pct(i['pct'])}" for i in snap["items"][:20]]
        return f"📈 榜单实时监控 {snap['updated_at'][11:16]}", (snap.get("price_label") or snap.get("status_text") or "") + "\n" + "\n".join(lines)
    if cmd == "相似前10":
        def job():
            from backend.app.api.routes import scan

            res = scan(None, 10, 0)
            return f"🔍 形态相似前10 {res.get('date', '')}", _scan_lines(res.get("items") or [])
        return _later(reply, job, "正在和样本池比相似度（约半分钟），完成后推给你")
    if cmd == "观察池":
        def job():
            from backend.app.api.routes import get_next_day_pool

            res = get_next_day_pool(None, 10)
            return f"🔭 次日观察池 {res.get('date', '')}", _scan_lines(res.get("items") or [], "prediction_score")
        return _later(reply, job, "正在计算次日观察池（约半分钟），完成后推给你")
    if cmd == "重新生成":
        def job():
            from backend.app.services.ranking_service import generate_ranking

            with session_scope() as s:
                board = generate_ranking(s, None, trigger="manual", prefer="auto")
            from backend.app.services.ranking_service import display_order

            return notify.ranking_text(display_order(dict(board)))
        return _later(reply, job, "正在重新生成 AI 榜单（可能要几分钟），完成后推给你")
    if cmd == "总结":
        from backend.app.models.tables import AiDailySummary

        with session_scope() as s:
            row = s.query(AiDailySummary).order_by(AiDailySummary.trade_date.desc()).first()
            payload = dict(row.payload) if row else None
        return notify.summary_text(payload) if payload else ("📝 每日总结", "还没有每日总结")
    if cmd in ("自选", "异动"):
        with session_scope() as s:
            snap = ws.snapshot(s)
        rows = snap["items"]
        if cmd == "异动":
            rows = [r for r in rows if r["pct"] is not None and abs(float(r["pct"])) >= 3]
        if not snap["items"]:
            return "⭐ 自选股", "自选股是空的。发「加自选 600519」添加"
        lines = [f"{notify.LEVEL_ICON.get(r['raw_level'], '▫️')} {notify.stock(r['name'], r['code'])} "
                 f"{r['price'] if r['price'] is not None else '—'} {notify.fmt_pct(r['pct'])}" for r in rows[:40]]
        head = snap.get("price_label") or ""
        title = f"⭐ 自选{'异动 ≥3%' if cmd == '异动' else '行情'} {snap['updated_at'][11:16]}"
        return title, (head + "\n" if head else "") + ("\n".join(lines) or "没有涨跌幅 ≥3% 的自选股")
    if cmd == "添加说明":
        return "➕ 添加自选", "发送：加自选 代码/名称\n例：加自选 600519 或 加自选 茅台（支持拼音首字母 gzmt）"
    if cmd == "删除说明":
        return "➖ 删除自选", "发送：删自选 代码/名称\n例：删自选 600519"
    if cmd == "导出自选":
        items = ws.load()
        body = "\n".join(f"{x['code']} {x.get('name', '')}" + (f" # {x['note']}" if x.get("note") else "") for x in items)
        return f"📤 自选股（{len(items)}只，可整段复制回网页一键导入）", body or "（空）"
    if cmd in ("加自选", "删自选"):
        if not arg:
            return "⭐ 自选股", f"请带上代码或名称，例如：{cmd} 600519"
        with session_scope() as s:
            hit, options = _resolve(s, arg)
        if hit is None:
            if not options:
                return "⭐ 自选股", f"没找到「{arg}」"
            return "⭐ 匹配到多只", "请写完整代码：\n" + "\n".join(f"{o['code']} {o.get('name', '')}" for o in options)
        label = notify.stock(hit.get("name", ""), hit["code"])
        if cmd == "加自选":
            res = ws.add([{"code": hit["code"], "name": hit.get("name", "")}])
            return "⭐ 自选股", f"{'已在自选里' if hit['code'] in res['duplicates'] else '✅ 已加入自选'}：{label}"
        if hit["code"] not in {x["code"] for x in ws.load()}:
            return "⭐ 自选股", f"{label} 不在自选里"
        ws.remove([hit["code"]])
        return "⭐ 自选股", f"🗑 已删除自选：{label}"
    if cmd == "采集":
        def job():
            from backend.app.services import manual_capture

            return notify.capture_text(manual_capture.capture())
        return _later(reply, job, "正在采集全市场实时行情（约半分钟），完成后推前10给你")
    if cmd == "同步":
        def job():
            from backend.app.services.sync_service import start_sync, status

            start_sync()
            deadline = _time.monotonic() + 3600
            while status().get("running") and _time.monotonic() < deadline:
                _time.sleep(5)
            st = status()
            icon = "✅" if st.get("phase") == "done" else "⚠️"
            return f"{icon} 同步行情", st.get("message") or st.get("phase") or ""
        return _later(reply, job, "已开始同步日线（几分钟），完成后告诉你结果")
    if cmd == "状态":
        return status_text()
    if cmd == "检查更新":
        from backend.app.services import update_service

        info = update_service.check()
        return update_service.wecom_text(info)
    return "📖 可用指令", HELP_BODY


def status_text() -> tuple[str, str]:
    from backend.app import __version__
    from backend.app.api.routes import health
    from backend.app.services.backtest_service import warm_status
    from backend.app.services.sync_service import status
    from backend.app.tasks import scheduler

    h, st, warm = health(), status(), warm_status()
    jobs = scheduler.next_runs()
    lines = [
        f"版本：v{__version__}",
        f"数据日期：{h.get('data_date') or '—'}（已存 {h.get('stored_days')} 个交易日）",
        f"行情源：{h.get('active_source') or h.get('data_source')}",
        f"AI 模型：{_model_line()}",
        f"同步：{'进行中 ' + str(st.get('progress')) + '%' if st.get('running') else (st.get('message') or '空闲')}",
        f"回测缓存：{'预热中' if warm.get('running') else ('出错 ' + warm['last_error'][:60] if warm.get('last_error') else '就绪')}",
    ]
    if jobs:
        lines.append("下次任务：")
        lines += [f"  {t} {name}" for name, t in jobs[:5]]
    return "🖥 系统状态", "\n".join(lines)


def _model_line() -> str:
    from backend.app.core import ai_profiles

    pub = ai_profiles.public()
    active = next((p for p in pub["items"] if p["active"]), None)
    if not active:
        return "未配置"
    return f"{active['name']}（{active['model']}）" + ("" if active["api_key_set"] else " 缺密钥") + (f"，共 {len(pub['items'])} 个可切换" if len(pub["items"]) > 1 else "")
