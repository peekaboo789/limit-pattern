"""AI 推荐出去的那些行，后来到底封没封板。

这张账是「它过去被推荐过」那一列的出处，也是评分模型里历史成功率那一项的原料。
对账只读库里已有的行情，一次网关都不敲，所以随时可以重跑，重跑不会多花钱。

判据说清三件事：
1. `trade_date` 存的是**推荐当日**（那份榜的交易日），不是判据日。判据日是推荐日之后
   库里出现的第一个交易日——行情还没进来的那些行永远停在 `pending`。
2. `pending` 永不进命中率的分母。没定判的账不是没中，是还不知道。
3. 从没被推荐过的代码在 `history_overview` 的返回值里根本不出现，
   证据行因此是 `ai_history = null`，不是一片 0。
"""

from __future__ import annotations

from datetime import date, datetime

from backend.app.core.clock import SHANGHAI
from backend.app.core.constants import TEXT_LIMIT
from backend.app.models.tables import AiPickResult, DailyQuote, Stock

VERDICT_HIT = "hit"
VERDICT_MISS = "miss"
VERDICT_PENDING = "pending"
VERDICTS = (VERDICT_HIT, VERDICT_MISS, VERDICT_PENDING)

#: 历史总分：自评均值乘一个用 Laplace 平滑过的命中率因子。
#: 一条都没定判时这个数就只是它自己的评分均值，页面上必须这么写。
HISTORY_FORMULA = "avg_score × (0.3 + 0.7 × (h+1)/(settled+2))"
ALL_PENDING_FORMULA = "样本全是待验证 — 这个数先只是它自评"
RECENT_RECORDS = 3
VERDICT_RULE = "判据 = 推荐日之后库里第一个交易日，daily_quotes.is_limit_up 是否为 1"


def _now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _text(value) -> str:
    text = str(value or "").strip()
    return text[:TEXT_LIMIT]


def hit_rate(hits: int, misses: int) -> float | None:
    """已定判里的命中率（百分数）。一条都没定判时给 None，不给 0。"""
    settled = hits + misses
    if settled <= 0:
        return None
    return round(hits * 100.0 / settled, 2)


def score_from_history(avg_score: float | None, hits: int, settled: int) -> int | None:
    if avg_score is None:
        return None
    avg = float(avg_score)
    if settled <= 0:
        return int(round(avg))
    factor = 0.3 + 0.7 * (hits + 1) / (settled + 2)
    return max(0, min(100, int(round(avg * factor))))


def stats_from(rows: list[AiPickResult]) -> dict:
    """把一只代码的若干行账折成一份统计。行序按 `recommended_at desc, id desc` 由调用方保证。"""
    hits = sum(1 for row in rows if row.verdict == VERDICT_HIT)
    misses = sum(1 for row in rows if row.verdict == VERDICT_MISS)
    pending = sum(1 for row in rows if row.verdict not in {VERDICT_HIT, VERDICT_MISS})
    settled = hits + misses
    scored = [float(row.score) for row in rows if isinstance(row.score, (int, float))]
    avg = round(sum(scored) / len(scored), 2) if scored else None
    return {
        "picks": len(rows),
        "hits": hits,
        "misses": misses,
        "pending": pending,
        "settled": settled,
        "hit_rate_percent": hit_rate(hits, misses),
        "avg_score": avg,
        "all_pending": settled == 0 and pending > 0,
        "history_score": score_from_history(avg, hits, settled),
        "formula": ALL_PENDING_FORMULA if settled == 0 and pending > 0 else HISTORY_FORMULA,
        "recent": [_row_view(row) for row in rows[:RECENT_RECORDS]],
    }


def _row_view(row: AiPickResult) -> dict:
    payload = dict(row.payload or {})
    return {
        "id": row.id,
        "report_id": row.report_id,
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "judge_date": payload.get("judge_date"),
        "recommended_at": row.recommended_at,
        "stock_code": row.stock_code,
        "stock_name": row.stock_name,
        "source": row.source,
        "model": row.model,
        "score": row.score,
        "criteria": row.criteria,
        "reason": row.reason,
        "counter_evidence": row.counter_evidence,
        "verdict": row.verdict,
        "checked_at": row.checked_at,
        "at": {
            "max_similarity": row.at_max_similarity,
            "composite_score": row.at_composite_score,
            "level": row.at_level,
            "model_gap": row.at_model_gap,
            "to_limit_percent": row.at_to_limit_percent,
            "change_percent": row.at_change_percent,
        },
        "actual": {
            "is_limit_up": row.actual_is_limit_up,
            "close": row.actual_close,
            "change_percent": row.actual_change_percent,
            "max_change_percent": row.actual_max_change_percent,
            "source": row.actual_source,
        },
    }


def record_picks(session, report: dict, *, source: str) -> int:
    """把一次花钱的结论落成账。同一份报告里重复的代码只留第一条。

    推荐当日取那份榜的交易日；判据日此刻还不知道，留给 `reconcile` 去认。
    """
    trade_day = _as_date(report.get("trade_date"))
    picks = report.get("picks") or []
    if trade_day is None or not picks:
        return 0
    names = {
        row[0]: row[1]
        for row in session.query(Stock.code, Stock.name).filter(Stock.code.in_([str(p.get("stock_code") or "") for p in picks]))
    }
    report_id = report.get("report_id")
    written = 0
    seen: set[str] = set()
    for pick in picks:
        code = str(pick.get("stock_code") or "")
        if not code or code in seen:
            continue
        seen.add(code)
        evidence = pick.get("evidence") or {}
        formula = evidence.get("formula") or {}
        session.add(
            AiPickResult(
                trade_date=trade_day,
                stock_code=code,
                source=source,
                score=None if pick.get("score") is None else float(pick["score"]),
                reason=_text(pick.get("reason")),
                verdict=VERDICT_PENDING,
                at_composite_score=formula.get("composite_score") if formula.get("has_signal") else None,
                at_level=str(formula.get("level") or ""),
                at_model_gap=formula.get("model_gap"),
                report_id=None if report_id is None else int(report_id),
                model=str(report.get("model") or ""),
                recommended_at=str(report.get("finished_at") or _now_iso()),
                stock_name=str(pick.get("stock_name") or names.get(code) or ""),
                criteria=str(report.get("criteria") or "")[:500],
                counter_evidence=_text(pick.get("counter_evidence")),
                at_max_similarity=evidence.get("max_similarity"),
                at_to_limit_percent=evidence.get("to_limit_percent"),
                at_change_percent=evidence.get("change_percent"),
                payload={"chain": source, "metric": str(report.get("metric") or "")},
            )
        )
        written += 1
    return written


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def history_overview(session, codes: list[str]) -> dict[str, dict]:
    """一批代码各自的历史账。从没被推荐过的代码不出现在返回值里。"""
    unique = sorted({str(code) for code in codes if code})
    if not unique:
        return {}
    rows = (
        session.query(AiPickResult)
        .filter(AiPickResult.stock_code.in_(unique))
        .order_by(AiPickResult.recommended_at.desc(), AiPickResult.id.desc())
        .all()
    )
    grouped: dict[str, list[AiPickResult]] = {}
    for row in rows:
        grouped.setdefault(row.stock_code, []).append(row)
    return {code: stats_from(items) for code, items in grouped.items()}


def pick_history(session, code: str) -> dict | None:
    """一只代码的完整账。从没推过就回 None——这是第一等答案，不是空表。"""
    code = str(code or "").strip()
    if not code:
        return None
    rows = (
        session.query(AiPickResult)
        .filter(AiPickResult.stock_code == code)
        .order_by(AiPickResult.recommended_at.desc(), AiPickResult.id.desc())
        .all()
    )
    if not rows:
        return None
    stats = stats_from(rows)
    latest = rows[0]
    payload = dict(latest.payload or {})
    return {
        "code": code,
        "stock_name": latest.stock_name or _name_of(session, code),
        "score": stats["history_score"],
        **{key: value for key, value in stats.items() if key != "recent"},
        "latest": {
            "trade_date": latest.trade_date.isoformat() if latest.trade_date else None,
            "judge_date": payload.get("judge_date"),
            "recommended_at": latest.recommended_at,
            "score": latest.score,
            "composite_score": latest.at_composite_score,
            "level": latest.at_level,
            "model_gap": latest.at_model_gap,
            "verdict": latest.verdict,
            "reason": latest.reason,
            "criteria": latest.criteria,
            "model": latest.model,
            "checked_at": latest.checked_at,
        },
    }


def _name_of(session, code: str) -> str:
    row = session.query(Stock.name).filter(Stock.code == code).one_or_none()
    return str(row[0]) if row else ""


def _judge_date(session, day: date) -> date | None:
    """推荐日之后库里出现的第一个交易日。行情还没同步就是 None，这一行继续待验证。"""
    row = (
        session.query(DailyQuote.trade_date)
        .filter(DailyQuote.trade_date > day)
        .order_by(DailyQuote.trade_date.asc())
        .first()
    )
    if row:
        return row[0]
    return None


def reconcile(session) -> dict:
    """把待验证的行和实际行情对一遍。只读库里已有的行，不敲网关。

    只挑 `pending`：已经定判的那一行行情不会自己变，重跑因此一条都不改，
    页面上「当场补一次对账」点两下不会把账改花。
    """
    rows = (
        session.query(AiPickResult)
        .filter(AiPickResult.verdict == VERDICT_PENDING)
        .order_by(AiPickResult.trade_date, AiPickResult.stock_code)
        .all()
    )
    judged = 0
    hits = 0
    misses = 0
    still_pending = 0
    for row in rows:
        if row.trade_date is None:
            continue
        judge = _judge_date(session, row.trade_date)
        if judge is None:
            still_pending += 1
            continue
        quote = (
            session.query(DailyQuote)
            .filter(DailyQuote.stock_code == row.stock_code, DailyQuote.trade_date == judge)
            .one_or_none()
        )
        if quote is None:
            # 那只股票在判据日根本没有行情（停牌或已退市），这一行永远定不了判。
            still_pending += 1
            continue
        payload = dict(row.payload or {})
        payload["judge_date"] = judge.isoformat()
        limit_up = bool(quote.is_limit_up)
        row.actual_is_limit_up = limit_up
        row.actual_close = quote.close
        row.actual_change_percent = quote.change_percent
        row.actual_max_change_percent = _max_change(quote)
        row.actual_source = str(quote.source or "")
        row.verdict = VERDICT_HIT if limit_up else VERDICT_MISS
        row.checked_at = _now_iso()
        row.payload = payload
        judged += 1
        hits += 1 if limit_up else 0
        misses += 0 if limit_up else 1
    return {
        "judged": judged,
        "hits": hits,
        "misses": misses,
        "still_pending": still_pending,
        "checked_at": _now_iso(),
    }


def _max_change(quote: DailyQuote) -> float | None:
    """判据日最高涨幅：(最高价 / 前收 − 1) × 100。前收缺就返回 None。"""
    if quote.high is None or not quote.pre_close:
        return None
    return round((float(quote.high) / float(quote.pre_close) - 1.0) * 100.0, 4)


def day_groups(session) -> list[dict]:
    """按判据日分组的账，给「建议 vs 实际」那张小表。"""
    rows = session.query(AiPickResult).order_by(AiPickResult.trade_date.desc()).all()
    grouped: dict[str, list[AiPickResult]] = {}
    for row in rows:
        key = str((row.payload or {}).get("judge_date") or "")
        if not key:
            key = f"待验证（{row.trade_date.isoformat() if row.trade_date else '?'} 之后没行情）"
        grouped.setdefault(key, []).append(row)
    items = []
    for key, items_in_day in grouped.items():
        hits = sum(1 for row in items_in_day if row.verdict == VERDICT_HIT)
        misses = sum(1 for row in items_in_day if row.verdict == VERDICT_MISS)
        pending = len(items_in_day) - hits - misses
        items.append(
            {
                "judge_date": key,
                "rows": len(items_in_day),
                "hits": hits,
                "misses": misses,
                "pending": pending,
                "hit_rate_percent": hit_rate(hits, misses),
            }
        )
    items.sort(key=lambda item: item["judge_date"], reverse=True)
    return items


def summary(session) -> dict:
    """对账总账 + 明细，全部零成本。"""
    rows = session.query(AiPickResult).all()
    hits = sum(1 for row in rows if row.verdict == VERDICT_HIT)
    misses = sum(1 for row in rows if row.verdict == VERDICT_MISS)
    pending = len(rows) - hits - misses
    settled = hits + misses
    checked = [row.checked_at for row in rows if row.checked_at]
    return {
        "check": {
            "total": len(rows),
            "hits": hits,
            "misses": misses,
            "pending": pending,
            "settled": settled,
            "hit_rate_percent": hit_rate(hits, misses),
            "last_checked_at": max(checked) if checked else None,
        },
        "days": day_groups(session),
        "verdict_rule": VERDICT_RULE,
        "formula": HISTORY_FORMULA,
        "disclaimer": "这是它过去说过的那些话后来对上了多少，不是明天会涨停的概率。",
    }


def result_rows(session, *, trade_date: str = "", verdict: str = "", limit: int = 200) -> dict:
    query = session.query(AiPickResult)
    day = _as_date(trade_date)
    if day is not None:
        query = query.filter(AiPickResult.trade_date == day)
    if verdict in VERDICTS:
        query = query.filter(AiPickResult.verdict == verdict)
    rows = query.order_by(AiPickResult.trade_date.desc(), AiPickResult.id.desc()).limit(limit).all()
    return {"items": [_row_view(row) for row in rows], "limit": limit, "total_read": len(rows)}
