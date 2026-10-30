"""导出给外部 AI 分析的输入（前 100 只），以及导入外部 AI 返回的前 20 JSON。

导出内容和应用内 AI 排名用的是同一套系统提示、字段和市场环境，只是一次给全 100 只。
"""

from __future__ import annotations

import json
import re
from datetime import date

from backend.app.core.config import read_settings_file
from backend.app.services.ranking_service import (
    KEEP,
    SYSTEM_PROMPT,
    _parse_entry,
    _save_ranking,
    candidates,
)
from backend.app.services.summary_service import AiFailed

EXPORT_TOP = 100
SNAPSHOT_TEXT = {
    "daily": "正式日线（收盘后数据）",
    "intraday": "盘中实时快照（成交量按 U 形分时曲线折算为全天估计）",
    "morning_freeze": "11:30 冻结快照（成交量为全天估计）",
    "close": "收盘报价（正式日线尚未同步）",
}

CLOSING = (
    "以上每只的「综合」已由系统按固定公式算好（见评分框架），排名以系统综合评分为准，你不需要也不能改它。\n"
    "请对全部候选逐只解读，挑出你认为最值得关注的前 {keep} 只，给出你自己的相对判断分 score（0–100，只作对照，页面显示为模型分和与系统分的偏差）。\n"
    "只返回一个 JSON，不要附加其他文字，格式：\n"
    '{{"date":"{day}","model":"你的模型名","items":[{{"code":"600000","score":72,'
    '"reason":"60字以内，写成模型依据，点出最关键的形态与量能特征","risk":"30字以内","tags":["缩量回调","均线多头"]}}]}}\n'
    "要求：code 必须是上面出现过的 6 位代码；score 为 0–100 的数字；tags 1–4 个；只用给出的数据，不编造新闻、公告、题材；"
    "null 不是 0 分；评分是形态复盘分，不是涨停概率，不写买入建议或目标价。"
)


def _version() -> str:
    from backend.app import __version__

    return __version__


def build_ai_input(session, day: date, snapshot: dict | None = None, top: int = EXPORT_TOP, fmt: str = "txt") -> tuple[str, str]:
    from backend.app.services.ai_context import LEGEND, market_context, market_line, stock_line

    snapshot = snapshot or {"kind": "daily"}
    from backend.app.services.similarity_service import feed_order

    pool = candidates(session, day, top)
    items = feed_order(pool["items"])  # 已涨停段在前，段内按最高相似度降序
    if not items:
        raise LookupError("这一天没有可导出的候选股票")
    settings = read_settings_file()
    dates = pool.get("pool_dates") or []
    skipped = len(pool.get("skipped") or [])
    kind = str(snapshot.get("kind") or "daily")
    bars_text = SNAPSHOT_TEXT.get(kind, kind)
    if snapshot.get("taken_at"):
        bars_text += f"，快照时间 {snapshot['taken_at']}"
    md = fmt == "md"
    h = (lambda t: f"## {t}") if md else (lambda t: f"【{t}】")
    header = [
        ("# " if md else "") + f"A 股涨停形态 AI 分析输入 · 评估日 {day.isoformat()}",
        f"- 评估日：{day.isoformat()}",
        f"- 数据源：{settings.get('active_source') or settings.get('data_source') or 'auto'}",
        f"- 样本池：{pool['pool_size']} 条（涨停 {pool['pool_size'] + skipped}，{skipped} 条缺前 3 日 K 线），"
        f"近 {len(dates)} 个交易日 {dates[0] if dates else '-'} 至 {dates[-1] if dates else '-'}",
        f"- K 线：{bars_text}",
        f"- 候选：全市场（含已涨停）按最高相似度前 {len(items)} 只，已涨停 {sum(1 for i in items if i.get('is_limit_up'))} 只排在前面",
        f"- 程序版本：{_version()}",
    ]
    lines = [*header, "", h("市场环境"), market_line(market_context(session, day)), "", h("系统提示与评分框架")]
    lines.append(SYSTEM_PROMPT)
    lines += ["", h("字段说明"), LEGEND, "", h(f"候选（{len(items)} 只，每行一只）")]
    if md:
        lines.append("```text")
    lines.extend(stock_line(item, item.get("metrics") or {}) for item in items)
    if md:
        lines.append("```")
    lines += ["", h("输出要求"), CLOSING.format(keep=KEEP, day=day.isoformat())]
    text = "\n".join(lines) + "\n"
    return text, f"ai_input_{day.isoformat()}.{'md' if md else 'txt'}"


# ---------------- 导入 ----------------

_CODE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def _loads_loose(text: str):
    """从任意文字里抠出 JSON：去掉 ``` 围栏、中文引号、尾逗号；对象或数组都行。"""
    raw = (text or "").strip().lstrip("\ufeff")
    fence = re.search(r"```(?:json|JSON)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    raw = raw.replace("“", '"').replace("”", '"').replace("，\"", ',"')
    decoder = json.JSONDecoder()
    for start, ch in sorted(((i, c) for c in "{[" if (i := raw.find(c)) >= 0)):
        chunk = raw[start:]
        for attempt in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
            try:
                value, _end = decoder.raw_decode(attempt)
                return value
            except ValueError:
                continue
    raise AiFailed("没有找到可解析的 JSON：请粘贴外部 AI 返回的 {\"items\":[...]} 原文")


def _entries(value) -> tuple[list, dict]:
    if isinstance(value, list):
        return value, {}
    if isinstance(value, dict):
        for key in ("items", "top20", "top", "ranking", "rankings", "results", "data", "stocks"):
            inner = value.get(key)
            if isinstance(inner, list):
                return inner, value
            if isinstance(inner, dict):
                got, _meta = _entries(inner)
                if got:
                    return got, value
    raise AiFailed("JSON 里没有 items 列表")


def _to_number(value):
    if isinstance(value, str):
        found = re.search(r"-?\d+(?:\.\d+)?", value)
        return float(found.group()) if found else None
    return value


def parse_external(text: str) -> tuple[dict[str, dict], dict]:
    value = _loads_loose(text)
    raw, meta = _entries(value)
    scores: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        found = _CODE.search(str(entry.get("code") or entry.get("代码") or entry.get("symbol") or "").zfill(6))
        if not found or found.group(1) in scores:
            continue
        entry = dict(entry)
        entry.setdefault("score", entry.get("总分") or entry.get("ai_score"))
        entry["score"] = _to_number(entry.get("score"))
        entry["risk_deduct"] = _to_number(entry.get("risk_deduct"))
        sub = entry.get("sub") or entry.get("subscores") or entry.get("dimensions")
        if isinstance(sub, dict):
            entry["sub"] = {k: _to_number(v) for k, v in sub.items()}
        if not entry.get("reason"):
            entry["reason"] = entry.get("理由") or ""
        parsed = _parse_entry(entry)
        if parsed:
            scores[found.group(1)] = parsed
    if not scores:
        raise AiFailed("没有解析出任何有效条目：每条需要 6 位 code，以及 score 或 reason")
    return scores, meta if isinstance(meta, dict) else {}


def import_external(session, text: str, day: date | None = None, model: str = "") -> dict:
    from backend.app.models.tables import Stock
    from backend.app.services.ai_context import market_context
    from backend.app.services.ranking_service import _latest_day
    from backend.app.services.retention import quote_dates

    scores, meta = parse_external(text)
    if day is None and meta.get("date"):
        try:
            day = date.fromisoformat(str(meta["date"])[:10])
        except ValueError:
            day = None
    if day is None or day not in set(quote_dates(session)):
        day = _latest_day(session)
    pool = candidates(session, day, EXPORT_TOP)
    names = {row.code: row.name for row in session.query(Stock.code, Stock.name).filter(Stock.code.in_(list(scores)))}
    known = {item["code"] for item in pool["items"]}
    unknown = [code for code in scores if code not in known and code not in names]
    for code in unknown:
        scores.pop(code)  # 本地完全没有这只股票，多半是外部 AI 编的
    if not scores:
        raise AiFailed("导入的代码都不在本地股票列表里")
    from backend.app.services.ranking_service import outside_items

    outside = outside_items(session, day, [code for code in scores if code not in known], names)
    extra = {
        "imported_count": len(scores),
        "outside_candidates": sorted(code for code in scores if code not in known),
        "dropped_unknown": unknown,
    }
    model = (model or str(meta.get("model") or "") or "外部 AI").strip()[:60]
    return _save_ranking(session, day, pool, market_context(session, day), scores, "external", {"kind": "daily", "taken_at": None, "count": 0}, model, extra, names, select="scores", outside=outside)
