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
    "🛠 工具：采集分析（一键采集+AI分析）/ 切换AI（回复数字换模型）/ 采集 / 同步 / 总结",
    "⭐ 自选：自选 / 异动 / 导出自选",
    "  加自选 代码或名称（如 加自选 600519、加自选 茅台、加自选 gzmt）",
    "  删自选 代码或名称（如 删自选 600519）",
    "🤖 多模型：开会验证 代码或名称 / 多模型验证 代码或名称 / 讨论验证 代码或名称",
    "🖥 其他：状态 / 检查更新",
    "也可以直接点对话框底部的菜单。",
])

# 菜单 key → 指令
MENU = {
    "RANK_TODAY": "榜单", "RANK_LIVE": "实时监控", "SIM_TOP": "相似前10", "NEXT_POOL": "观察池", "RANK_REGEN": "重新生成",
    "WL_QUOTE": "自选", "WL_MOVERS": "异动", "WL_ADD_HELP": "添加说明", "WL_DEL_HELP": "删除说明", "WL_EXPORT": "导出自选",
    "CAPTURE": "采集", "CAPTURE_AI": "采集分析", "AI_SWITCH": "切换AI", "HELP": "帮助", "SYNC": "同步", "SUMMARY": "总结", "STATUS": "状态", "UPDATE": "检查更新",
}
# 文字指令（含别名），长的放前面先匹配
ALIASES = [
    ("加自选", "加自选"), ("添加自选", "加自选"), ("删自选", "删自选"), ("删除自选", "删自选"),
    ("导出自选", "导出自选"), ("自选异动", "异动"), ("异动", "异动"), ("自选行情", "自选"), ("自选", "自选"),
    ("今日AI榜单", "榜单"), ("AI榜单", "榜单"), ("重新生成榜单", "重新生成"), ("重新生成", "重新生成"), ("榜单", "榜单"),
    ("实时监控", "实时监控"), ("监控", "实时监控"), ("相似前10", "相似前10"), ("相似", "相似前10"),
    ("次日观察池", "观察池"), ("观察池", "观察池"),
    ("一键采集+AI分析", "采集分析"), ("一键采集", "采集分析"), ("采集+AI分析", "采集分析"), ("采集分析", "采集分析"), ("采集AI", "采集分析"),
    ("切换AI", "切换AI"), ("切换模型", "切换AI"), ("AI列表", "切换AI"), ("模型列表", "切换AI"), ("模型", "切换AI"),
    ("采集当前行情", "采集"), ("采集", "采集"), ("同步行情", "同步"), ("同步", "同步"),
    ("今日总结", "总结"), ("总结", "总结"), ("系统状态", "状态"), ("状态", "状态"),
    ("检查更新", "检查更新"), ("更新", "检查更新"),
    ("开会验证", "开会验证"), ("多模型验证", "开会验证"), ("讨论验证", "开会验证"),
    ("帮助", "帮助"), ("菜单", "帮助"),
    ("信号报告", "信号报告"), ("信号有效性", "信号报告"), ("最新信号", "最新信号"), ("盯盘状态", "盯盘状态"),
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


# ---------- v0.5.4 切换 AI：发列表，回复数字切换（每人 5 分钟内有效） ----------
PICK_TTL = 300
_PICK: dict[str, tuple[float, list[str]]] = {}
_PICK_LOCK = threading.Lock()


def pending_pick(user: str) -> bool:
    with _PICK_LOCK:
        hit = _PICK.get((user or "").lower())
        if hit and hit[0] < _time.monotonic():
            _PICK.pop((user or "").lower(), None)
            hit = None
    return bool(hit)


def ai_list(user: str = "") -> tuple[str, str]:
    from backend.app.core import ai_profiles

    pub = ai_profiles.public()
    items = pub["items"]
    if not items:
        return "🤖 切换AI", "还没有配置 AI 模型，请在网页 设置 → AI 模型 里添加"
    lines = [f"{n}. {'✅' if p['active'] else '▫️'} {p['name'] or p['model']}（{p['model'] or '未填模型'}）{'' if p['api_key_set'] else ' ⚠️缺密钥'}"
             for n, p in enumerate(items, 1)]
    with _PICK_LOCK:
        _PICK[(user or "").lower()] = (_time.monotonic() + PICK_TTL, [p["id"] for p in items])
    return "🤖 切换AI（✅=当前）", "\n".join(lines) + "\n\n回复数字切换（5 分钟内有效）"


def pick(user: str, text: str) -> tuple[str, str]:
    from backend.app.core import ai_profiles

    key = (user or "").lower()
    with _PICK_LOCK:
        hit = _PICK.get(key)
        if not hit or hit[0] < _time.monotonic():
            _PICK.pop(key, None)
            return "🤖 切换AI", "列表已过期，请重新发「切换AI」"
        ids = hit[1]
    try:
        n = int(text)
    except (TypeError, ValueError):
        return "🤖 切换AI", f"请回复 1~{len(ids)} 之间的数字"
    if not 1 <= n <= len(ids):
        return "🤖 切换AI", f"没有第 {n} 个，请回复 1~{len(ids)} 之间的数字"
    try:
        pub = ai_profiles.activate(ids[n - 1])
    except LookupError:
        return "🤖 切换AI", "这个模型配置已被删除，请重新发「切换AI」"
    with _PICK_LOCK:
        _PICK.pop(key, None)
    cur = next(p for p in pub["items"] if p["active"])
    return "✅ 已切换AI", f"当前模型：{cur['name'] or cur['model']}（{cur['model']}）" + ("" if cur["api_key_set"] else "\n⚠️ 这个配置还没填密钥，AI 分析会失败")


# ---------- v0.5.4 一键采集 + AI 分析 ----------
_CAP_AI = threading.Lock()


def capture_ai_job() -> list[tuple[str, str]]:
    """采集当前行情 → 前 100 → 当前 AI 模型打分 → 出 AI 榜单（和网页里「生成 AI 榜单」同一套逻辑）。"""
    from backend.app.services import manual_capture
    from backend.app.services.ai_context import market_line
    from backend.app.services.ranking_service import _rank, display_order, generate_ranking
    from backend.app.services.summary_service import ai_ready

    if not ai_ready():
        return [("❌ 采集分析没开始", "当前 AI 模型没有配置好（缺接口地址 / 模型 / 密钥）。发「切换AI」换一个，或到网页 设置 → AI 模型 里填写")]
    note = ""
    try:
        cap = manual_capture.capture()
        data_label = cap.get("label") or cap.get("date") or ""
        with session_scope() as s:
            payload = manual_capture.load_snapshot()
            with manual_capture.snapshot_day(s, payload) as (day, info):
                board = _rank(s, day, None, "manual", info, True)
    except manual_capture.NoDataYet as exc:
        note = f"（{exc}）"
        with session_scope() as s:
            board = generate_ranking(s, None, trigger="manual", prefer="auto")
        snap = board.get("snapshot") or {}
        data_label = f"{board.get('date', '')} " + ("收盘日线（非交易时段，用最近一个交易日的数据）" if snap.get("kind") in (None, "daily", "close") else (snap.get("label") or ""))
    board = display_order(dict(board))
    items = board.get("items") or []
    lines = [f"数据时间：{data_label}{note}"]
    if isinstance(board.get("market"), dict):
        try:
            lines.append(market_line(board["market"]))
        except Exception:  # noqa: BLE001
            pass
    if board.get("ai_error"):
        lines.append("⚠️ " + board["ai_error"])
    lines.append("")
    for it in items[:20]:
        ms, cs = it.get("model_score"), it.get("composite_score")
        row = f"{it.get('rank'):>2}. {notify.stock(it.get('name'), it['code'])} 综合 {'—' if cs is None else round(float(cs), 1)} AI {'—' if ms is None else round(float(ms), 1)}"
        if it.get("reason"):
            row += f"\n    {str(it['reason'])[:60]}"
        lines.append(row)
    lines.append("\n注：相似度只是形态接近程度，不是上涨概率；只用评估日及以前的数据，不构成投资建议。")
    return [(f"🤖 采集+AI分析 前20（共{len(items)}只）", "\n".join(lines))]


def start_capture_ai(reply: Reply | None):
    if not _CAP_AI.acquire(blocking=False):
        return "⏳ 正在分析", "上一次「一键采集+AI分析」还没做完，完成后会推给你"

    def job():
        try:
            return capture_ai_job()
        finally:
            _CAP_AI.release()

    if reply is None:
        out = job()
        return out[0]

    def run():
        try:
            for msg in job():
                reply(*msg)
        except Exception as exc:  # noqa: BLE001
            reply("❌ 采集分析失败", str(exc)[:300])

    try:  # 先回「已开始」再开工，保证顺序
        reply("⏳ 已开始", "已开始一键采集+AI分析：采集全市场行情 → 选前100 → 当前AI模型打分（几分钟），完成后推前20给你")
    finally:
        threading.Thread(target=run, name="wecom-capture-ai", daemon=True).start()
    return None


def start_council(arg: str, reply: Reply | None = None):
    """先回已开始，后台跑完再回一句简洁结果；完整过程在网页查看。"""
    if not arg:
        return "🤖 多模型讨论", "请带上股票代码或名称，例如：开会验证 600519"

    def job():
        from backend.app.services.council import service

        try:
            result = service.start(arg)
        except (ValueError, LookupError) as exc:
            return "❌ 讨论没开始", str(exc)
        except RuntimeError as exc:
            return "⏳ 讨论没开始", str(exc)
        session_id = result["session_id"]
        deadline = _time.monotonic() + 3600
        row = None
        while _time.monotonic() < deadline:
            _time.sleep(3)
            row = service.get(session_id)
            if row and row["status"] in ("done", "failed"):
                break
        if row is None:
            return "🤖 多模型讨论", "讨论状态没取到，请在网页查看"
        if row["status"] == "failed":
            return "❌ 多模型讨论", row.get("error") or "讨论失败"
        conclusion = row.get("conclusion") or {}
        prob = conclusion.get("up_probability")
        confidence = {"low": "低", "medium": "中", "high": "高"}.get(conclusion.get("confidence"), "低")
        summary = conclusion.get("summary") or ""
        disagree = conclusion.get("disagreements") or []
        risks = conclusion.get("risks") or []
        parts = [
            f"名称/代码：{row['stock_name']} {row['stock_code']}",
            (f"次日上涨主观估计：约 {prob}%（信心 {confidence}，未经统计校准）" if prob is not None else "次日上涨估计：证据不足或单位不明，暂不可估计"),
        ]
        if summary:
            parts.append(f"综述：{summary}")
        if disagree:
            parts.append("主要分歧：" + "；".join(disagree[:2]))
        if risks:
            parts.append("风险：" + "；".join(risks[:2]))
        parts.append("这是讨论会估计，不计入综合评分，不是投资建议。完整过程请在网页「多模型讨论验证」查看。")
        return "🤖 多模型讨论完成", "\n".join(parts)

    if reply is None:
        return job()

    def run_job():
        try:
            reply(*job())
        except Exception as exc:  # noqa: BLE001
            reply("❌ 多模型讨论失败", str(exc)[:300])

    participants = []
    try:
        from backend.app.services.council import service

        profiles = service._valid_profiles(None)
        participants = [f"{p['name'] or p['model']}" for p in profiles]
    except Exception:  # noqa: BLE001
        pass
    start_text = "已开始多模型讨论验证，正在多源核对行情/K线/资金/交易/新闻公告/财报；参会模型：" + ("、".join(participants) if participants else "使用已保存顺序") + "。完成后推结果给你"
    threading.Thread(target=run_job, name="wecom-council", daemon=True).start()
    return "⏳ 已开始", start_text

def run(cmd: str, arg: str = "", reply: Reply | None = None, user: str = "") -> tuple[str, str]:
    from backend.app.services import watchlist_service as ws

    if cmd == "采集分析":
        return start_capture_ai(reply)
    if cmd == "切换AI":
        return ai_list(user)
    if cmd == "选AI":
        return pick(user, arg)

    if cmd == "帮助":
        return "📖 可用指令", HELP_BODY
    if cmd == "开会验证":
        return start_council(arg, reply)
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
    if cmd == "盯盘状态":
        return signal_watch_status()
    if cmd == "最新信号":
        return signal_latest()
    if cmd == "信号报告":
        return signal_report_text(arg)
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


# ---------- v0.6.0 盘中信号 ----------
def signal_watch_status() -> tuple[str, str]:
    """盯盘状态：当前是否交易时段、各信号是否可推送、影子状态。"""
    from backend.app.services.monitor_service import alert_phase
    from backend.app.services.signals import shadow

    phase = alert_phase()
    with session_scope() as s:
        statuses = shadow.all_statuses(s)
    lines = [f"当前：{phase['text']}"]
    if not statuses:
        lines.append("还没有任何信号进入影子模式")
    else:
        for sig_type, status in sorted(statuses.items()):
            icon = {"live": "🟢", "passed": "🟡", "shadow": "⚪", "degraded": "🟠", "disabled": "🔴"}.get(status, "⚪")
            lines.append(f"{icon} {sig_type}：{status}")
    return "📡 盯盘状态", "\n".join(lines)


def signal_latest() -> tuple[str, str]:
    """最新信号：最近一个信号日的事件列表（只读，不提醒）。"""
    from backend.app.models.tables import SignalEvent, Stock

    with session_scope() as s:
        row = s.query(SignalEvent).order_by(SignalEvent.signal_date.desc(), SignalEvent.id.desc()).first()
        if row is None:
            return "🔔 最新信号", "还没有任何信号事件。先在影子模式下积累样本。"
        day = row.signal_date
        events = (
            s.query(SignalEvent)
            .filter(SignalEvent.signal_date == day)
            .order_by(SignalEvent.signal_type, SignalEvent.id)
            .all()
        )
        names = {r.code: r.name for r in s.query(Stock)}
    lines = [f"信号日 {day.isoformat()}（共 {len(events)} 条）"]
    for e in events[:20]:
        conf = "—" if e.confidence is None else f"{e.confidence:.0f}"
        lines.append(f"• {e.signal_type} {notify.stock(names.get(e.stock_code, ''), e.stock_code)} 把握 {conf}")
    lines.append("注：置信度不是概率，不构成投资建议。")
    return "🔔 最新信号", "\n".join(lines)


def signal_report_text(arg: str) -> tuple[str, str]:
    """信号报告：某类信号（或全部）的有效性统计。"""
    from backend.app.services.signals import report as signal_report

    signal_type = arg.strip() or None
    with session_scope() as s:
        rep = signal_report.build_report(s, signal_type)
    if rep.get("empty"):
        return "📊 信号报告", rep.get("disclaimer") or "还没有信号事件"
    lines = [f"信号 {rep.get('signal_type') or '全部'}（事件 {rep['n_events']} 条）"]
    by_type = rep.get("by_type") or {}
    if not by_type:
        lines.append("还没有可统计的信号类型")
    for sig_type, body in by_type.items():
        lines.append(f"【{sig_type}】事件 {body['n_events']} 条")
        for hold in (1, 3, 5):
            h = body["holds"].get(hold)
            if not h or h.get("n") == 0:
                continue
            sig = "✅" if h.get("significant_bh") else "▫️"
            lines.append(
                f"T+{hold} 超额均值 {h['excess_mean']}%（中位 {h['excess_median']}%），胜率 {h['win_rate']}，"
                f"p={h['p_value']}，BH校正 {'显著' if h.get('significant_bh') else '不显著'} {sig}"
            )
    lines.append("注：超额收益 = 个股收益 − 同期全市场等权中位数，是历史统计，不构成投资建议。")
    return "📊 信号报告", "\n".join(lines[:40])
