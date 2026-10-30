"""AI 前 20 榜单：取当前最相似的前 100 只，交给已配置的 AI 打 0-100 分，留前 20。

只用已经收盘或当前时刻之前的行情，不看未来。相似度不是涨停概率。
"""

from __future__ import annotations

import json
from datetime import date, datetime

from backend.app.core.clock import SHANGHAI, today
from backend.app.core.config import rankings_dir, read_settings_file
from backend.app.core.logging import get_logger
from backend.app.models.tables import AiRanking, DailyQuote
from backend.app.services.retention import quote_dates
from backend.app.services.similarity_service import DISCLAIMER, board_name, scan_market
from backend.app.services.summary_service import AiFailed, AiNotConfigured, _extract_json, ai_ready, call_chat

logger = get_logger("ranking")

CANDIDATES = 100
KEEP = 20

SYSTEM_PROMPT = (
    "你是A股形态统计助手。用户给出按三日形态相似度排出的候选股票，"
    "数据只有价格和成交量，没有新闻、公告和财报，不要编造。"
    "请给每只股票打 0 到 100 的形态评分，并给一句 30 字以内的理由。"
    "评分只反映形态和量能与近三日涨停样本的接近程度，不是涨停概率。"
    '只返回 JSON：{"items":[{"code":"600000","score":88,"reason":"..."}]}'
)


class RankingError(RuntimeError):
    pass


def _recent_bars(session, code: str, day: date, count: int = 3) -> list[dict]:
    rows = (
        session.query(DailyQuote)
        .filter(DailyQuote.stock_code == code, DailyQuote.trade_date <= day)
        .order_by(DailyQuote.trade_date.desc())
        .limit(count)
        .all()
    )
    return [
        {
            "date": row.trade_date.isoformat(),
            "close": row.close,
            "pct": row.change_percent,
            "volume": row.volume,
            "turnover": row.turnover_rate,
        }
        for row in reversed(rows)
    ]


def candidates(session, day: date, limit: int = CANDIDATES) -> dict:
    payload = scan_market(session, day, limit, 0)
    for item in payload["items"]:
        item["board_name"] = board_name(item["code"])
        item["bars"] = _recent_bars(session, item["code"], day)
    return payload


def _prompt(day: date, items: list[dict]) -> str:
    rows = [
        {
            "code": item["code"],
            "name": item["name"],
            "similarity": item["score"],
            "shape": item["shape_score"],
            "volume": item["volume_score"],
            "volume_pct": item["volume_pct_score"],
            "best_match": item["best_match"]["code"],
            "bars": item.get("bars") or [],
        }
        for item in items
    ]
    return f"评估日 {day.isoformat()}，共 {len(rows)} 只候选：\n" + json.dumps(rows, ensure_ascii=False)


def _parse_scores(text: str) -> dict[str, dict]:
    payload = _extract_json(text)
    raw = payload.get("items")
    if not isinstance(raw, list):
        raise AiFailed("AI 没有返回 items 列表")
    scores: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip()[-6:]
        try:
            score = float(entry.get("score"))
        except (TypeError, ValueError):
            continue
        score = max(0.0, min(100.0, score))
        reason = str(entry.get("reason") or "").strip()[:80]
        if code and code not in scores:
            scores[code] = {"score": round(score, 1), "reason": reason}
    if not scores:
        raise AiFailed("AI 没有给出有效评分")
    return scores


def _latest_day(session) -> date:
    days = quote_dates(session, today())
    if not days:
        raise LookupError("还没有行情")
    return days[-1]


def generate_ranking(session, day: date | None = None, *, chat=None, trigger: str = "manual") -> dict:
    if chat is None and not ai_ready():
        raise AiNotConfigured("未配置AI接口")
    day = day or _latest_day(session)
    if day > today():
        raise ValueError("不能用未来的日期")
    pool = candidates(session, day)
    items = pool["items"]
    if not items:
        raise LookupError("这一天没有可打分的候选股票")
    prompt = _prompt(day, items)
    text = chat(prompt) if chat is not None else call_chat(prompt, system=SYSTEM_PROMPT, timeout=180.0)
    scores = _parse_scores(text)
    ranked = []
    for item in items:
        got = scores.get(item["code"])
        if got is None:
            continue
        ranked.append(
            {
                "code": item["code"],
                "name": item["name"],
                "board_name": item["board_name"],
                "ai_score": got["score"],
                "reason": got["reason"],
                "similarity": item["score"],
                "shape_score": item["shape_score"],
                "volume_score": item["volume_score"],
                "volume_pct_score": item["volume_pct_score"],
                "best_match": item["best_match"],
                "close": (item.get("bars") or [{}])[-1].get("close"),
            }
        )
    ranked.sort(key=lambda row: (-row["ai_score"], -row["similarity"], row["code"]))
    ranked = ranked[:KEEP]
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    run_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    payload = {
        "date": day.isoformat(),
        "run_at": run_at,
        "trigger": trigger,
        "model": "" if chat is not None else str(read_settings_file().get("ai_model") or ""),
        "candidate_count": len(items),
        "pool_dates": pool["pool_dates"],
        "pool_size": pool["pool_size"],
        "disclaimer": DISCLAIMER,
        "items": ranked,
    }
    session.add(AiRanking(trade_date=day, run_at=run_at, trigger=trigger, payload=payload))
    session.commit()
    _write_file(session, day)
    return payload


def _runs(session, day: date) -> list[AiRanking]:
    return session.query(AiRanking).filter(AiRanking.trade_date == day).order_by(AiRanking.id).all()


def _write_file(session, day: date) -> None:
    runs = [row.payload for row in _runs(session, day)]
    if not runs:
        return
    document = {"date": day.isoformat(), "latest": runs[-1], "runs": runs}
    path = rankings_dir() / f"{day.isoformat()}.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_ranking(session, day: date | None = None) -> dict | None:
    query = session.query(AiRanking)
    if day is not None:
        query = query.filter(AiRanking.trade_date == day)
    row = query.order_by(AiRanking.trade_date.desc(), AiRanking.id.desc()).first()
    if row is None:
        return None
    payload = dict(row.payload)
    payload["runs"] = [item.run_at for item in _runs(session, row.trade_date)]
    return payload


def ranking_dates(session) -> list[str]:
    rows = session.query(AiRanking.trade_date).distinct().all()
    return sorted({row[0].isoformat() for row in rows}, reverse=True)


def previous_ranking(session, moment_day: date | None = None) -> dict | None:
    """监控用：今天之前最近一个交易日的最终榜单（当天最后一次运行）。"""
    moment_day = moment_day or today()
    row = (
        session.query(AiRanking)
        .filter(AiRanking.trade_date < moment_day)
        .order_by(AiRanking.trade_date.desc(), AiRanking.id.desc())
        .first()
    )
    return None if row is None else dict(row.payload)


def scheduled_ranking(trigger: str = "schedule") -> dict | None:
    from backend.app.core.database import session_scope

    if not ai_ready():
        logger.info("没配 AI，跳过定时榜单")
        return None
    if today().weekday() >= 5:
        return None
    try:
        with session_scope() as session:
            return generate_ranking(session, trigger=trigger)
    except (AiFailed, AiNotConfigured, LookupError, ValueError) as exc:
        logger.warning("定时榜单没有生成：%s", exc)
        return None
