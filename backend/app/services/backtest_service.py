"""回测：评估日 D 只用 D 及以前的行情打分，再看 D 的下一个交易日实际表现。严格不看未来。

样本受明细保留天数限制（默认一周），只能算最近几天，结论仅供参考。
历史发生率不是单只股票的涨停概率。
"""

from __future__ import annotations

from datetime import date

from backend.app.models.tables import AiRanking, DailyQuote
from backend.app.services.retention import quote_dates
from backend.app.services.similarity_service import DISCLAIMER, scan_market

BANDS = ((90.0, "90+"), (80.0, "80-90"), (70.0, "70-80"), (0.0, "<70"))


def band_of(score: float) -> str:
    for low, label in BANDS:
        if score >= low:
            return label
    return "<70"


def _next_outcomes(session, day: date) -> dict[str, dict]:
    rows = session.query(DailyQuote).filter(DailyQuote.trade_date == day).all()
    return {
        row.stock_code: {
            "limit_up": bool(row.is_limit_up),
            "pct": row.change_percent,
            "high_pct": (row.high - row.pre_close) / row.pre_close * 100 if row.high and row.pre_close else None,
        }
        for row in rows
        if row.source not in ("intraday", "morning_freeze", "close")
    }


def _empty_band(label: str) -> dict:
    return {"band": label, "count": 0, "limit_up": 0, "up3": 0, "pct_sum": 0.0, "pct_n": 0}


def _finish(stats: dict) -> dict:
    count = stats["count"]
    return {
        "band": stats["band"],
        "count": count,
        "limit_up": stats["limit_up"],
        "limit_up_rate": round(stats["limit_up"] / count * 100, 1) if count else None,
        "up3_rate": round(stats["up3"] / count * 100, 1) if count else None,
        "avg_next_pct": round(stats["pct_sum"] / stats["pct_n"], 2) if stats["pct_n"] else None,
    }


def _add(stats: dict, outcome: dict | None) -> None:
    if outcome is None:
        return
    stats["count"] += 1
    stats["limit_up"] += int(outcome["limit_up"])
    if outcome.get("high_pct") is not None and outcome["high_pct"] >= 3:
        stats["up3"] += 1
    if outcome.get("pct") is not None:
        stats["pct_sum"] += float(outcome["pct"])
        stats["pct_n"] += 1


def similarity_backtest(session, top: int = 50) -> dict:
    days = [day for day in quote_dates(session)]
    bands = {label: _empty_band(label) for _low, label in BANDS}
    per_day = []
    for index in range(len(days) - 1):
        day, nxt = days[index], days[index + 1]
        scan = scan_market(session, day, top, 0)
        if not scan["items"]:
            continue
        outcomes = _next_outcomes(session, nxt)
        day_stats = _empty_band("all")
        for item in scan["items"]:
            outcome = outcomes.get(item["code"])
            _add(bands[band_of(item["score"])], outcome)
            _add(day_stats, outcome)
        per_day.append({"date": day.isoformat(), "next_date": nxt.isoformat(), **_finish(day_stats)})
    return {
        "kind": "similarity",
        "top": top,
        "disclaimer": DISCLAIMER + "；历史发生率不是单只股票的概率",
        "days": per_day,
        "bands": [_finish(bands[label]) for _low, label in BANDS],
    }


def ranking_backtest(session) -> dict:
    days = quote_dates(session)
    rows = session.query(AiRanking).order_by(AiRanking.trade_date, AiRanking.id).all()
    last_per_day: dict[date, dict] = {}
    for row in rows:
        last_per_day[row.trade_date] = row.payload
    total = _empty_band("all")
    per_day = []
    for day, payload in sorted(last_per_day.items()):
        later = [d for d in days if d > day]
        if not later:
            continue
        nxt = later[0]
        outcomes = _next_outcomes(session, nxt)
        stats = _empty_band("all")
        hits = []
        for item in payload.get("items") or []:
            outcome = outcomes.get(item["code"])
            _add(stats, outcome)
            _add(total, outcome)
            if outcome and outcome["limit_up"]:
                hits.append(item["code"])
        per_day.append({"date": day.isoformat(), "next_date": nxt.isoformat(), "hits": hits, **_finish(stats)})
    return {
        "kind": "ai_ranking",
        "disclaimer": "AI 评分不是涨停概率；历史发生率不是单只股票的概率",
        "days": per_day,
        "total": _finish(total),
    }


def next_day_pool(session, day: date | None = None, top: int = 30) -> dict:
    """次日观察池：评估日扫描结果，附上该分档在最近几天的历史发生率。不是预测。"""
    days = quote_dates(session)
    if not days:
        return {"date": None, "items": [], "bands": [], "disclaimer": DISCLAIMER}
    day = day or days[-1]
    history = similarity_backtest(session, top=max(top, 50))
    rates = {band["band"]: band for band in history["bands"]}
    scan = scan_market(session, day, top, 0)
    items = []
    for item in scan["items"]:
        band = rates.get(band_of(item["score"])) or {}
        items.append(
            {
                **item,
                "band": band_of(item["score"]),
                "band_samples": band.get("count", 0),
                "band_limit_up_rate": band.get("limit_up_rate"),
                "band_up3_rate": band.get("up3_rate"),
            }
        )
    return {
        "date": day.isoformat(),
        "pool_dates": scan["pool_dates"],
        "disclaimer": "观察池按相似度挑选，附历史发生率；不是涨停概率，也不是买卖建议",
        "bands": history["bands"],
        "items": items,
    }
