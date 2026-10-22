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


class AiNotConfigured(RuntimeError):
    pass


class AiFailed(RuntimeError):
    pass


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
    return {
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
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise AiFailed("模型没有返回 JSON")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AiFailed("模型返回的 JSON 无法解析") from exc
    if not isinstance(payload, dict):
        raise AiFailed("模型返回的不是对象")
    return payload


def call_chat(prompt: str) -> str:
    settings = read_settings_file()
    url = str(settings.get("ai_base_url") or "").rstrip("/")
    key = str(settings.get("ai_api_key") or "")
    model = str(settings.get("ai_model") or "")
    if not (url and key and model):
        raise AiNotConfigured("未配置AI接口")
    endpoint = url if url.endswith("/chat/completions") else url + "/chat/completions"
    try:
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是行情统计助手。只根据用户给的数字写总结。"
                            "不要预测涨停，不要编写新闻、公告或财报。"
                            "只返回 JSON，包含 headline 和 key_points。key_points 是 3 到 5 条字符串。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=90.0,
        )
    except httpx.HTTPError as exc:
        raise AiFailed("AI 接口没有连上") from exc
    if response.status_code >= 400:
        raise AiFailed(f"AI 接口返回 HTTP {response.status_code}")
    body = response.json()
    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise AiFailed("AI 接口没有返回文本") from exc


def ai_ready() -> bool:
    settings = read_settings_file()
    return bool(settings.get("ai_base_url") and settings.get("ai_model") and settings.get("ai_api_key"))


def statistical_points(facts: dict) -> list[str]:
    points = [facts["headline"]]
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
    prompt = "下面是已经算好的涨停统计，请只据此写 headline 和 key_points：\n" + json.dumps(
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
        payload["key_points"] = [item.strip() for item in points][:5]
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
