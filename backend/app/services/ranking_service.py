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
    "评分只反映形态和量能与近 30 个交易日涨停样本的接近程度，不是涨停概率。"
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


BATCH_SIZE = 20
PARALLEL = 3
PROGRESS: dict = {"running": False, "done": 0, "total": 0, "failed": 0}


def progress() -> dict:
    return dict(PROGRESS)


def _prompt(day: date, items: list[dict]) -> str:
    """紧凑表格：每只一行，省掉 JSON 键名，提示短很多。"""
    lines = ["code|name|sim|shape|vol|volpct|三日收盘|三日涨跌%|三日换手%"]
    for item in items:
        bars = item.get("bars") or []
        closes = "/".join("" if bar.get("close") is None else f"{bar['close']:.2f}" for bar in bars)
        pcts = "/".join("" if bar.get("pct") is None else f"{bar['pct']:.1f}" for bar in bars)
        turns = "/".join("" if bar.get("turnover") is None else f"{bar['turnover']:.1f}" for bar in bars)
        lines.append(
            f"{item['code']}|{item['name']}|{item['score']:.0f}|{item['shape_score']:.0f}|"
            f"{item['volume_score']:.0f}|{item['volume_pct_score']:.0f}|{closes}|{pcts}|{turns}"
        )
    return f"评估日 {day.isoformat()}，候选 {len(items)} 只（sim=综合相似度，0-100）：\n" + "\n".join(lines)


def _score_batches(day: date, items: list[dict], chat) -> dict[str, dict]:
    """每批 20 只、最多 3 批并发；某一批失败时其余批照常，全部失败才报错。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    batches = [items[i : i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    PROGRESS.update(running=True, done=0, total=len(batches), failed=0)

    def run(batch: list[dict]) -> dict[str, dict]:
        prompt = _prompt(day, batch)
        text = chat(prompt) if chat is not None else call_chat(prompt, system=SYSTEM_PROMPT, timeout=150.0)
        return _parse_scores(text)

    scores: dict[str, dict] = {}
    errors: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            futures = [pool.submit(run, batch) for batch in batches]
            for future in as_completed(futures):
                try:
                    scores.update(future.result())
                except (AiFailed, AiNotConfigured) as exc:
                    errors.append(str(exc))
                    PROGRESS["failed"] += 1
                PROGRESS["done"] += 1
    finally:
        PROGRESS["running"] = False
    if not scores:
        raise AiFailed(errors[0] if errors else "AI 没有给出有效评分")
    return scores


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


def generate_ranking(
    session,
    day: date | None = None,
    *,
    chat=None,
    trigger: str = "manual",
    moment: datetime | None = None,
    prefer: str = "auto",
) -> dict:
    """day 为空时用「此刻」：交易时段用盘中快照当临时日线。指定 day 时只用那天及以前的正式日线。"""
    from backend.app.services.intraday_service import provisional_day

    if chat is None and not ai_ready():
        raise AiNotConfigured("未配置AI接口")
    if day is not None:
        if day > today():
            raise ValueError("不能用未来的日期")
        return _rank(session, day, chat, trigger, {"kind": "daily", "taken_at": None, "count": 0})
    with provisional_day(session, moment, prefer) as (as_of, info):
        if as_of is None:
            raise LookupError("还没有行情")
        return _rank(session, as_of, chat, trigger, info)


def _rank(session, day: date, chat, trigger: str, snapshot: dict) -> dict:
    pool = candidates(session, day)
    items = pool["items"]
    if not items:
        raise LookupError("这一天没有可打分的候选股票")
    scores = _score_batches(day, items, chat)
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
        "snapshot": snapshot,
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
    """监控用：按交易日历取上一交易日的最终榜单（当天最后一次运行）。
    上一交易日没有榜单时，退回到更早最近的一份，并在 payload 里标 stale。"""
    from backend.app.core.calendar import previous_trading_day

    moment_day = moment_day or today()
    target = previous_trading_day(moment_day)
    exact = (
        session.query(AiRanking)
        .filter(AiRanking.trade_date == target)
        .order_by(AiRanking.id.desc())
        .first()
    )
    if exact is not None:
        return dict(exact.payload)
    row = (
        session.query(AiRanking)
        .filter(AiRanking.trade_date < target)
        .order_by(AiRanking.trade_date.desc(), AiRanking.id.desc())
        .first()
    )
    if row is None:
        return None
    payload = dict(row.payload)
    payload["stale"] = True
    payload["expected_date"] = target.isoformat()
    return payload


def scheduled_ranking(trigger: str = "schedule") -> dict | None:
    from backend.app.core.database import session_scope

    if not ai_ready():
        logger.info("没配 AI，跳过定时榜单")
        return None
    from backend.app.core.calendar import is_trading_day

    if not is_trading_day(today()):
        logger.info("今天休市，跳过定时榜单")
        return None
    try:
        with session_scope() as session:
            return generate_ranking(session, trigger=trigger)
    except (AiFailed, AiNotConfigured, LookupError, ValueError) as exc:
        logger.warning("定时榜单没有生成：%s", exc)
        return None
