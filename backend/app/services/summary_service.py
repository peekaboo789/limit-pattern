"""每日涨停总结。数字由本系统先算好，模型只写标题和要点。未配置时不调用。"""

from __future__ import annotations

import json
from datetime import date, datetime

import httpx

from backend.app.core.clock import SHANGHAI
from backend.app.core.config import read_settings_file, summaries_dir
from backend.app.models.tables import AiDailySummary, DailyQuote
from backend.app.providers.markets import is_in_scope
from backend.app.services.similarity_service import board_name

REQUIRED_KEYS = (
    "date",
    "limitup_count",
    "sectors",
    "ladder",
    "patterns",
    "volume_features",
    "key_points",
    "model",
    "generated_at",
)


from backend.app.core.ai_client import AiFailed, AiNotConfigured  # noqa: E402,F401


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 4)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 4)


def _streak(session, code: str, day: date) -> int:
    rows = (
        session.query(DailyQuote.trade_date)
        .filter(DailyQuote.stock_code == code, DailyQuote.is_limit_up.is_(True), DailyQuote.trade_date <= day)
        .all()
    )
    owned = {row[0] for row in rows}
    days = [
        row[0]
        for row in session.query(DailyQuote.trade_date)
        .filter(DailyQuote.trade_date <= day)
        .distinct()
        .order_by(DailyQuote.trade_date)
    ]
    if day not in owned:
        return 0
    index = days.index(day) if day in days else None
    if index is None:
        return 1
    streak = 1
    while index > 0 and days[index - 1] in owned:
        streak += 1
        index -= 1
    return streak


def _market_review_facts(session, day: date) -> tuple[dict, list[dict], list[dict]]:
    """情绪、高度、炸板率、龙头形态，全部来自本地日线。"""
    from backend.app.services.ai_context import load_bars, market_context, stock_metrics
    from backend.app.services.retention import quote_dates

    market = market_context(session, day)
    leaders = []
    for code in market.get("leaders") or []:
        metrics = stock_metrics(load_bars(session, code, day, 30))
        recent = metrics.get("recent") or []
        leaders.append(
            {
                "code": code,
                "height": market.get("max_height"),
                "last3_pct": [None if r["pct"] is None else round(r["pct"], 2) for r in recent[-3:]],
                "last3_turnover": [None if r["turn"] is None else round(r["turn"], 2) for r in recent[-3:]],
                "last_volume_ratio": None if not recent or recent[-1]["vr"] is None else round(recent[-1]["vr"], 2),
                "upper_shadow": None if metrics.get("upper_shadow") is None else round(metrics["upper_shadow"], 2),
            }
        )
    days = [d for d in quote_dates(session, day) if d < day][-3:]
    history = []
    for d in days:
        ctx = market_context(session, d)
        history.append({k: ctx[k] for k in ("date", "limit_up", "limit_down", "broken_rate", "max_height", "up_down_ratio")})
    return market, leaders, history


def build_facts(session, day: date) -> dict:
    rows = (
        session.query(DailyQuote)
        .filter(DailyQuote.trade_date == day, DailyQuote.is_limit_up.is_(True))
        .order_by(DailyQuote.stock_code)
        .all()
    )
    rows = [row for row in rows if is_in_scope(row.stock_code)]
    sectors: dict[str, int] = {}
    ladders: dict[int, list[str]] = {}
    patterns: dict[str, list[str]] = {}
    turnovers: list[float] = []
    volumes: list[float] = []
    for row in rows:
        name = board_name(row.stock_code)
        sectors[name] = sectors.get(name, 0) + 1
        height = _streak(session, row.stock_code, day)
        ladders.setdefault(height, []).append(row.stock_code)
        if row.turnover_rate is not None:
            turnovers.append(row.turnover_rate)
        if row.volume is not None:
            volumes.append(row.volume)
        previous = (
            session.query(DailyQuote)
            .filter(DailyQuote.stock_code == row.stock_code, DailyQuote.trade_date < day)
            .order_by(DailyQuote.trade_date.desc())
            .first()
        )
        label = "普通涨停"
        if previous and previous.volume and row.volume:
            if row.volume >= previous.volume * 1.3:
                label = "放量上攻"
            elif row.volume <= previous.volume * 0.8:
                label = "缩量板"
        patterns.setdefault(label, []).append(row.stock_code)
    headline = f"{day.isoformat()} 共 {len(rows)} 只涨停"
    market, leaders, history = _market_review_facts(session, day)
    return {
        "market": market,
        "leaders": leaders,
        "recent_market": history,
        "date": day.isoformat(),
        "limitup_count": len(rows),
        "sectors": [{"name": name, "count": count} for name, count in sorted(sectors.items())],
        "ladder": [
            {"height": height, "count": len(codes), "codes": codes}
            for height, codes in sorted(ladders.items())
        ],
        "patterns": [
            {"name": name, "count": len(codes), "example_code": codes[0]}
            for name, codes in sorted(patterns.items())
        ],
        "volume_features": {
            "median_turnover_rate": _median(turnovers),
            "median_volume": _median(volumes),
            "sample_count": len(rows),
        },
        "headline": headline,
        "key_points": [headline],
    }


def _extract_json(text: str) -> dict:
    """容错解析：去掉 <think> 思考段、优先取 ```json 围栏，允许尾逗号。"""
    import re

    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    fenced = re.findall(r"```(?:json|JSON)?\s*(.*?)```", text, flags=re.S)
    candidates = [block for block in fenced if "{" in block] + [text]
    last_error: Exception | None = None
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        body = candidate[start : end + 1]
        for attempt in (body, re.sub(r",\s*([}\]])", r"\1", body)):
            try:
                payload = json.loads(attempt)
            except json.JSONDecodeError as exc:
                last_error = exc
                continue
            if isinstance(payload, dict):
                return payload
            raise AiFailed("模型返回的不是对象")
    if last_error is not None:
        raise AiFailed("模型返回的 JSON 无法解析") from last_error
    raise AiFailed("模型没有返回 JSON")


SUMMARY_SYSTEM = (
    "你是一名资深 A 股短线交易与涨停板研究员，多年复盘涨停板、连板、首板、炸板、分歧转一致、龙头股和情绪周期。"
    "现在根据用户给出的、由本地日线算好的当日统计写盘后复盘。只用给出的数字，不得编造新闻、公告、题材、政策或基本面。"
    "复盘要覆盖：情绪（涨跌家数、涨跌比、中位涨跌）、高度（最高连板及龙头的形态）、炸板率与封板质量、连板梯队结构、"
    "涨停股量能特征（放量/缩量板、换手）、和前几个交易日的对比（如给出）。"
    "不要预测明日涨跌，不要给买卖建议。"
    '只返回 JSON：{"headline":"一句话标题","key_points":["3 到 6 条要点"],'
    '"market_review":{"sentiment":"情绪…","height":"高度…","broken":"炸板…","leaders":"龙头形态…"}}'
)


def call_chat(prompt: str, system: str | None = None, timeout: float = 90.0) -> str:
    from backend.app.core.ai_client import chat

    return chat(
        system or SUMMARY_SYSTEM,
        prompt,
        settings=read_settings_file(),
        read_timeout=timeout,
    )


def ai_ready() -> bool:
    settings = read_settings_file()
    return bool(settings.get("ai_base_url") and settings.get("ai_model") and settings.get("ai_api_key"))


def statistical_review(facts: dict) -> dict:
    m = facts.get("market") or {}
    if not m:
        return {}
    leaders = facts.get("leaders") or []
    lead_text = "；".join(
        f"{x['code']} 近三日涨跌 {'/'.join('-' if v is None else str(v) for v in x['last3_pct'])}%，量比 {x['last_volume_ratio'] if x['last_volume_ratio'] is not None else '-'}"
        for x in leaders[:3]
    )
    return {
        "sentiment": f"上涨 {m.get('up')} / 下跌 {m.get('down')}，涨跌比 {m.get('up_down_ratio') if m.get('up_down_ratio') is not None else '-'}，中位涨跌 {m.get('median_pct') if m.get('median_pct') is not None else '-'}%",
        "height": f"最高 {m.get('max_height')} 连板",
        "broken": f"涨停 {m.get('limit_up')}、炸板 {m.get('broken')}，炸板率 {m.get('broken_rate') if m.get('broken_rate') is not None else '-'}%，跌停 {m.get('limit_down')}",
        "leaders": lead_text or "-",
    }


def statistical_points(facts: dict) -> list[str]:
    points = [facts["headline"]]
    m = facts.get("market") or {}
    if m:
        points.append(
            f"情绪：涨 {m.get('up')} 跌 {m.get('down')}，涨停 {m.get('limit_up')} 跌停 {m.get('limit_down')}，"
            f"炸板率 {m.get('broken_rate') if m.get('broken_rate') is not None else '-'}%，最高 {m.get('max_height')} 连板"
        )
    if facts["sectors"]:
        text = "，".join(f"{item['name']} {item['count']} 只" for item in facts["sectors"])
        points.append(f"板块：{text}")
    if facts["ladder"]:
        text = "，".join(f"{item['height']} 连板 {item['count']} 只" for item in facts["ladder"])
        points.append(f"连板：{text}")
    volume = facts["volume_features"]
    if volume.get("median_turnover_rate") is not None:
        points.append(f"涨停股换手率中位数 {volume['median_turnover_rate']}")
    if volume.get("median_volume") is not None:
        points.append(f"涨停股成交量中位数 {volume['median_volume']}")
    return points


def _statistics_payload(facts: dict) -> dict:
    return {
        "date": facts["date"],
        "limitup_count": facts["limitup_count"],
        "sectors": facts["sectors"],
        "ladder": facts["ladder"],
        "patterns": facts["patterns"],
        "volume_features": facts["volume_features"],
        "market": facts.get("market") or {},
        "market_review": statistical_review(facts),
        "key_points": statistical_points(facts),
        "headline": facts["headline"],
        "model": "",
        "source": "statistics",
        "ai_error": None,
        "generated_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
    }


def _save_summary(session, day: date, payload: dict) -> dict:
    row = session.get(AiDailySummary, day)
    if row is None:
        row = AiDailySummary(trade_date=day, payload=payload)
        session.add(row)
    else:
        row.payload = payload
    session.commit()
    path = summaries_dir() / f"{day.isoformat()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def generate_summary(session, day: date, chat=None) -> dict:
    """先落统计版。配了 AI 再补标题和要点；模型失败时仍保留统计版。"""
    facts = build_facts(session, day)
    payload = _statistics_payload(facts)
    if not ai_ready() and chat is None:
        return _save_summary(session, day, payload)
    prompt = "下面是已经算好的当日统计（含全市场情绪、连板梯队、龙头近三日形态、前几日对比），请据此写复盘：\n" + json.dumps(
        facts, ensure_ascii=False
    )
    try:
        text = chat(prompt) if chat is not None else call_chat(prompt)
        written = _extract_json(text)
    except (AiNotConfigured, AiFailed) as exc:
        payload["ai_error"] = str(exc)
        return _save_summary(session, day, payload)
    points = written.get("key_points")
    headline = written.get("headline")
    if isinstance(points, list) and all(isinstance(item, str) and item.strip() for item in points):
        payload["key_points"] = [item.strip() for item in points][:6]
    review = written.get("market_review")
    if isinstance(review, dict):
        merged = dict(payload.get("market_review") or {})
        for key in ("sentiment", "height", "broken", "leaders"):
            value = review.get(key)
            if isinstance(value, str) and value.strip():
                merged[key] = value.strip()[:200]
        payload["market_review"] = merged
    if isinstance(headline, str) and headline.strip():
        payload["headline"] = headline.strip()
    settings = read_settings_file()
    payload["model"] = str(settings.get("ai_model") or "")
    payload["source"] = "ai"
    payload["ai_error"] = None
    return _save_summary(session, day, payload)


def refresh_latest_summary() -> dict | None:
    """同步结束后重写最近一个交易日的总结。"""
    from backend.app.core.clock import today
    from backend.app.core.database import session_scope
    from backend.app.services.retention import quote_dates

    with session_scope() as session:
        days = [item for item in quote_dates(session) if item <= today()]
        if not days:
            return None
        return generate_summary(session, days[-1])


def backfill_summaries(rewrite: bool = False) -> int:
    """样本池里没有总结的日子补一份统计版（不调 AI，省钱省时）；最新一天由 refresh_latest_summary 写。"""
    from backend.app.core.clock import today
    from backend.app.core.config import sample_pool_days
    from backend.app.core.database import session_scope
    from backend.app.services.retention import quote_dates

    count = 0
    with session_scope() as session:
        days = [d for d in quote_dates(session) if d <= today()][-sample_pool_days():-1]
        for day in days:
            if not rewrite and session.get(AiDailySummary, day) is not None:
                continue
            _save_summary(session, day, _statistics_payload(build_facts(session, day)))
            count += 1
    return count


def ensure_daily_summary(day: date | None = None) -> dict | None:
    """没有这一天的总结才写。给收盘后的定时任务补漏。"""
    from backend.app.core.clock import today
    from backend.app.core.database import session_scope
    from backend.app.services.retention import quote_dates

    with session_scope() as session:
        if day is None:
            days = [item for item in quote_dates(session) if item <= today()]
            if not days:
                return None
            day = days[-1]
        elif day > today():
            return None
        existing = session.get(AiDailySummary, day)
        if existing is not None:
            return dict(existing.payload or {})
        return generate_summary(session, day)


def list_summaries(session) -> dict:
    rows = session.query(AiDailySummary).order_by(AiDailySummary.trade_date.desc()).all()
    items = []
    for row in rows:
        payload = row.payload or {}
        items.append(
            {
                "date": row.trade_date.isoformat(),
                "limitup_count": int(payload.get("limitup_count") or 0),
                "headline": str(payload.get("headline") or ""),
            }
        )
    return {"items": items}


def get_summary(session, day: date) -> dict | None:
    row = session.get(AiDailySummary, day)
    if row is None:
        return None
    return dict(row.payload or {})
