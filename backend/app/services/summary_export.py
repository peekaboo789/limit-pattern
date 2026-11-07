"""v0.4.7 每日总结按自定义区间一键导出：TXT / Markdown / JSON / CSV。

总结永久保存（数据库 ai_daily_summaries + data/summaries/*.json），不受日线保留天数限制。"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta

from backend.app.core.calendar import is_trading_day
from backend.app.core.clock import SHANGHAI
from backend.app.core.config import summaries_dir
from backend.app.models.tables import AiDailySummary

FORMATS = ("txt", "md", "json", "csv")
MAX_SPAN_DAYS = 366 * 5  # 最多 5 年（约 1200 个交易日），再大就请分段
BOM = "\ufeff"
REVIEW_LABELS = (("sentiment", "情绪"), ("height", "高度"), ("broken", "炸板"), ("leaders", "龙头形态"))


def _load(session, start: date, end: date) -> dict[date, dict]:
    found: dict[date, dict] = {}
    rows = session.query(AiDailySummary).filter(AiDailySummary.trade_date >= start, AiDailySummary.trade_date <= end).all()
    for row in rows:
        found[row.trade_date] = dict(row.payload or {})
    # 数据库被清过、只剩文件时也能导
    try:
        for path in summaries_dir().glob("????-??-??.json"):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if start <= day <= end and day not in found:
                found[day] = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        pass
    return dict(sorted(found.items()))


def earliest(session) -> date | None:
    row = session.query(AiDailySummary.trade_date).order_by(AiDailySummary.trade_date).first()
    days = [row[0]] if row else []
    try:
        for path in summaries_dir().glob("????-??-??.json"):
            try:
                days.append(date.fromisoformat(path.stem))
            except ValueError:
                pass
    except OSError:
        pass
    return min(days) if days else None


def validate(start: date | None, end: date | None) -> tuple[date, date]:
    if start is None or end is None:
        raise ValueError("请给出开始和结束日期（YYYY-MM-DD）")
    if start > end:
        raise ValueError(f"开始日期 {start} 晚于结束日期 {end}")
    if (end - start).days > MAX_SPAN_DAYS:
        raise ValueError(f"区间太长（{(end - start).days + 1} 天），一次最多导出 5 年，请分段导出")
    return start, end


def collect(session, start: date, end: date) -> dict:
    start, end = validate(start, end)
    summaries = _load(session, start, end)
    trading = []
    cursor = start
    while cursor <= end:
        if is_trading_day(cursor):
            trading.append(cursor)
        cursor += timedelta(days=1)
    missing = [d for d in trading if d not in summaries]
    return {
        "start": start,
        "end": end,
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "trading_days": len(trading),
        "summaries": summaries,
        "missing": missing,
    }


def _stats(day: date, p: dict) -> dict:
    m = p.get("market") or {}
    ladder = p.get("ladder") or []
    top = max((int(x.get("height") or 0) for x in ladder), default=None)
    return {
        "date": day.isoformat(),
        "limit_up": p.get("limitup_count", m.get("limit_up")),
        "limit_down": m.get("limit_down"),
        "broken": m.get("broken"),
        "broken_rate": m.get("broken_rate"),
        "max_height": m.get("max_height", top),
        "up": m.get("up"),
        "down": m.get("down"),
        "median_pct": m.get("median_pct"),
        "leaders": ",".join(m.get("leaders") or []),
        "headline": p.get("headline") or "",
        "source": p.get("source") or "",
    }


CSV_COLUMNS = [("date", "日期"), ("limit_up", "涨停"), ("limit_down", "跌停"), ("broken", "炸板"), ("broken_rate", "炸板率%"),
               ("max_height", "最高连板"), ("up", "上涨"), ("down", "下跌"), ("median_pct", "中位涨跌%"), ("leaders", "龙头"),
               ("headline", "标题"), ("source", "来源")]


def _fmt(v) -> str:
    return "—" if v is None or v == "" else str(v)


def _day_lines(day: date, p: dict, md: bool) -> list[str]:
    h = (lambda t: f"## {t}") if md else (lambda t: f"==== {t} ====")
    sub = (lambda t: f"### {t}") if md else (lambda t: f"【{t}】")
    li = "- "
    out = [h(f"{day.isoformat()}  {p.get('headline') or ''}".rstrip()), ""]
    m = p.get("market") or {}
    out.append(sub("市场"))
    if m:
        out.append(f"{li}涨 {_fmt(m.get('up'))} / 跌 {_fmt(m.get('down'))} / 平 {_fmt(m.get('flat'))} / 停牌 {_fmt(m.get('suspended'))}（共 {_fmt(m.get('listed') or m.get('stocks'))}），涨跌比 {_fmt(m.get('up_down_ratio'))}，中位涨跌 {_fmt(m.get('median_pct'))}%")
        out.append(f"{li}涨停 {_fmt(p.get('limitup_count', m.get('limit_up')))}，跌停 {_fmt(m.get('limit_down'))}，炸板 {_fmt(m.get('broken'))}（炸板率 {_fmt(m.get('broken_rate'))}%），最高 {_fmt(m.get('max_height'))} 连板，龙头 {_fmt(','.join(m.get('leaders') or []))}")
    else:
        out.append(f"{li}涨停 {_fmt(p.get('limitup_count'))}")
    review = p.get("market_review") or {}
    if review:
        out.append(sub("市场复盘"))
        for key, label in REVIEW_LABELS:
            if review.get(key):
                out.append(f"{li}{label}：{review[key]}")
    if p.get("key_points"):
        out.append(sub("要点"))
        out += [f"{li}{x}" for x in p["key_points"]]
    for key, label in (("summary", "AI 总结"), ("ai_summary", "AI 总结"), ("text", "总结"), ("analysis", "分析")):
        if isinstance(p.get(key), str) and p[key].strip():
            out.append(sub(label))
            out.append(p[key].strip())
    if p.get("sectors"):
        out.append(sub("板块"))
        out.append("、".join(f"{x.get('name')} {x.get('count')}" for x in p["sectors"]))
    if p.get("ladder"):
        out.append(sub("连板梯队"))
        out += [f"{li}{x.get('height')} 连板 {x.get('count')} 只：{','.join(x.get('codes') or [])}" for x in p["ladder"]]
    if p.get("patterns"):
        out.append(sub("形态"))
        out.append("、".join(f"{x.get('name')} {x.get('count')}（例 {x.get('example_code')}）" for x in p["patterns"]))
    vf = p.get("volume_features") or {}
    if vf:
        out.append(sub("量能"))
        out.append(f"{li}换手率中位数 {_fmt(vf.get('median_turnover_rate'))}，成交量中位数 {_fmt(vf.get('median_volume'))}，样本 {_fmt(vf.get('sample_count'))}")
    meta = [f"来源 {p.get('source') or '—'}"]
    if p.get("model"):
        meta.append(f"模型 {p['model']}")
    if p.get("generated_at"):
        meta.append(f"生成于 {p['generated_at']}")
    if p.get("ai_error"):
        meta.append(f"AI 未成功：{p['ai_error']}")
    out.append(sub("说明"))
    out.append("；".join(meta))
    # 其余未识别的字段也带上，保证“存了什么就导什么”
    known = {"date", "limitup_count", "sectors", "ladder", "patterns", "volume_features", "market", "market_review", "key_points",
             "headline", "model", "source", "ai_error", "generated_at", "summary", "ai_summary", "text", "analysis"}
    rest = {k: v for k, v in p.items() if k not in known}
    if rest:
        out.append(sub("其他字段"))
        out.append(("```json\n" if md else "") + json.dumps(rest, ensure_ascii=False, indent=2) + ("\n```" if md else ""))
    out.append("")
    return out


def _aggregate(data: dict, md: bool) -> list[str]:
    rows = [_stats(d, p) for d, p in data["summaries"].items()]
    if not rows:
        return []
    title = "## 区间汇总" if md else "==== 区间汇总 ===="
    out = [title, ""]
    nums = lambda key: [r[key] for r in rows if isinstance(r[key], (int, float))]
    lu, br, mh = nums("limit_up"), nums("broken_rate"), nums("max_height")
    if lu:
        out.append(f"- 日均涨停 {sum(lu) / len(lu):.1f} 只（最多 {max(lu)}，最少 {min(lu)}）")
    if br:
        out.append(f"- 平均炸板率 {sum(br) / len(br):.1f}%（最高 {max(br)}%，最低 {min(br)}%）")
    if mh:
        out.append(f"- 最高连板 {max(mh)}（区间内每天最高连板的平均 {sum(mh) / len(mh):.1f}）")
    out.append("")
    if md:
        out += ["| 日期 | 涨停 | 跌停 | 炸板率% | 最高连板 |", "| --- | ---: | ---: | ---: | ---: |"]
        out += [f"| {r['date']} | {_fmt(r['limit_up'])} | {_fmt(r['limit_down'])} | {_fmt(r['broken_rate'])} | {_fmt(r['max_height'])} |" for r in rows]
    else:
        out.append("日期\t涨停\t跌停\t炸板率%\t最高连板")
        out += [f"{r['date']}\t{_fmt(r['limit_up'])}\t{_fmt(r['limit_down'])}\t{_fmt(r['broken_rate'])}\t{_fmt(r['max_height'])}" for r in rows]
    out.append("")
    return out


def render(data: dict, fmt: str) -> tuple[str, str, str]:
    """返回 (正文, media_type, 文件名)。TXT / CSV 带 UTF-8 BOM，记事本和 Excel 直接认中文。"""
    if fmt not in FORMATS:
        raise ValueError("格式只能是 txt、md、json、csv")
    s, e = data["start"].isoformat(), data["end"].isoformat()
    name = f"每日总结_{s}_{e}.{fmt}"
    summaries = data["summaries"]
    if fmt == "json":
        doc = {"start": s, "end": e, "generated_at": data["generated_at"], "count": len(summaries),
               "trading_days": data["trading_days"], "missing": [d.isoformat() for d in data["missing"]],
               "items": [dict(p, date=p.get("date") or d.isoformat()) for d, p in summaries.items()]}
        return json.dumps(doc, ensure_ascii=False, indent=2) + "\n", "application/json; charset=utf-8", name
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow([label for _k, label in CSV_COLUMNS])
        for d, p in summaries.items():
            row = _stats(d, p)
            writer.writerow(["" if row[k] is None else row[k] for k, _l in CSV_COLUMNS])
        return BOM + buf.getvalue(), "text/csv; charset=utf-8", name
    md = fmt == "md"
    missing = data["missing"]
    head = [
        ("# " if md else "") + f"每日总结 {s} 至 {e}",
        "",
        f"- 区间：{s} 至 {e}（{data['trading_days']} 个交易日）",
        f"- 有总结：{len(summaries)} 天",
        f"- 缺总结：{len(missing)} 天" + (f"（{'、'.join(d.isoformat() for d in missing)}）" if missing else ""),
        f"- 导出时间：{data['generated_at']}",
        "",
    ]
    body = head
    for d, p in summaries.items():
        body += _day_lines(d, p, md)
    body += _aggregate(data, md)
    text = "\n".join(body).rstrip() + "\n"
    if md:
        return text, "text/markdown; charset=utf-8", name
    return BOM + text.replace("\n", "\r\n"), "text/plain; charset=utf-8", name
