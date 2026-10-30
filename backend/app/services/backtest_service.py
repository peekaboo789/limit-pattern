"""回测：评估日 D 只用 D 及以前的行情打分，再看 D 的下一个交易日实际表现。严格不看未来。

样本受明细保留天数限制（默认一周），只能算最近几天，结论仅供参考。
历史发生率不是单只股票的涨停概率。
"""

from __future__ import annotations

from datetime import date

from backend.app.models.tables import AiRanking, DailyQuote
from backend.app.services.retention import quote_dates
from backend.app.services.similarity_service import DISCLAIMER, scan_market

from backend.app.core.constants import MIN_BAND_OBSERVATIONS

BAND_FLOORS = (90.0, 80.0, 70.0, 60.0)
BACKTEST_DISCLAIMER = (
    "本模块检验的是「形态相似度」与「下一交易日涨停」的历史统计关联性，"
    "输出的历史实际发生率不是预测保证，不构成投资建议。"
)


def band_floors_from_settings() -> tuple[float, float, float, float]:
    from backend.app.core.config import similarity_threshold_high

    high = similarity_threshold_high()
    floors = sorted({high, 80.0, 70.0, 60.0}, reverse=True)
    return tuple(floors[:4]) if len(floors) >= 4 else BAND_FLOORS


def band_of(score: float, floors: tuple[float, ...] = BAND_FLOORS) -> str:
    """→ "90~100" / "80~90" / "70~80" / "60~70" / "<60"，下界含。"""
    for index, floor in enumerate(floors):
        if score >= floor:
            upper = 100 if index == 0 else floors[index - 1]
            return f"{floor:g}~{upper:g}"
    return f"<{floors[-1]:g}"


def band_labels(floors: tuple[float, ...] = BAND_FLOORS) -> list[str]:
    return [band_of(f, floors) for f in floors] + [f"<{floors[-1]:g}"]


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


def _finish(stats: dict, baseline: float | None = None) -> dict:
    count = stats["count"]
    rate = stats["limit_up"] / count * 100 if count else None
    enough = count >= MIN_BAND_OBSERVATIONS
    return {
        "band": stats["band"],
        "sample_count": count,
        "limit_up_count": stats["limit_up"],
        "enough": enough,
        "band_lift": round(rate / baseline, 4) if enough and rate is not None and baseline else None,
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


BACKTEST_DAYS = 10
_CACHE: dict = {}


def similarity_backtest(session, top: int = 50) -> dict:
    """最近 10 个评估日；一次性读入日线、不联网补拉；结果按 (最新日, 行数, top) 缓存。"""
    from sqlalchemy import func

    from backend.app.services.history_backfill import bulk_bars

    days = [day for day in quote_dates(session)]
    if not days:
        return {"kind": "similarity", "top": top, "disclaimer": BACKTEST_DISCLAIMER, "baseline_rate": None, "days": [], "bands": [_finish(_empty_band(l)) for l in band_labels(band_floors_from_settings())]}
    key = (days[-1], session.query(func.count(DailyQuote.id)).scalar(), top, band_floors_from_settings())
    if _CACHE.get("key") == key:
        return _CACHE["value"]
    session.info["lp_no_backfill"] = True
    try:
        with bulk_bars(session, days[-1]):
            value = _similarity_backtest(session, days[-(BACKTEST_DAYS + 1):], top)
    finally:
        session.info.pop("lp_no_backfill", None)
    _CACHE.update(key=key, value=value)
    return value


def _similarity_backtest(session, days: list, top: int) -> dict:
    """每个评估日对全部候选按最高相似度分档，统计次日涨停发生率；基准 = 全部候选的次日涨停率。"""
    floors = band_floors_from_settings()
    labels = band_labels(floors)
    bands = {label: _empty_band(label) for label in labels}
    total = _empty_band("all")
    per_day = []
    for index in range(len(days) - 1):
        day, nxt = days[index], days[index + 1]
        scan = scan_market(session, day, 100000, 0, details=False)
        if not scan["items"]:
            continue
        outcomes = _next_outcomes(session, nxt)
        day_stats = _empty_band("all")
        top_stats = _empty_band(f"top{top}")
        for rank, item in enumerate(scan["items"]):
            outcome = outcomes.get(item["code"])
            _add(bands[band_of(item["max_similarity"], floors)], outcome)
            _add(day_stats, outcome)
            _add(total, outcome)
            if rank < top:
                _add(top_stats, outcome)
        per_day.append({"date": day.isoformat(), "next_date": nxt.isoformat(), **_finish(day_stats), "top": _finish(top_stats)})
    baseline = total["limit_up"] / total["count"] * 100 if total["count"] else None
    return {
        "kind": "similarity",
        "top": top,
        "disclaimer": BACKTEST_DISCLAIMER,
        "band_floors": list(floors),
        "min_band_observations": MIN_BAND_OBSERVATIONS,
        "baseline_rate": None if baseline is None else round(baseline, 2),
        "days": per_day,
        "bands": [_finish(bands[label], baseline) for label in labels],
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


def band_lifts(session) -> tuple[dict[str, float | None], dict]:
    history = similarity_backtest(session, top=100)
    return {band["band"]: band.get("band_lift") for band in history["bands"]}, history


def prediction_for(item: dict, lifts: dict, floors=None) -> dict:
    from backend.app.services.score_model import prediction_score

    floors = floors or band_floors_from_settings()
    return prediction_score(item.get("max_similarity"), item.get("carry_rate"), lifts.get(band_of(item.get("max_similarity") or 0, floors)))


def next_day_pool(session, day: date | None = None, top: int = 30) -> dict:
    """次日观察池：评估日最高相似度前 N，附档位历史发生率与预测分。不是预测保证。"""
    days = quote_dates(session)
    if not days:
        return {"date": None, "items": [], "bands": [], "disclaimer": BACKTEST_DISCLAIMER}
    day = day or days[-1]
    lifts, history = band_lifts(session)
    rates = {band["band"]: band for band in history["bands"]}
    floors = band_floors_from_settings()
    scan = scan_market(session, day, top, 0)
    items = []
    for item in scan["items"]:
        label = band_of(item["max_similarity"], floors)
        band = rates.get(label) or {}
        items.append(
            {
                **item,
                "band_range": label,
                "band_samples": band.get("sample_count", 0),
                "band_limit_up_rate": band.get("limit_up_rate"),
                "band_up3_rate": band.get("up3_rate"),
                **prediction_for(item, lifts, floors),
            }
        )
    from backend.app.core.constants import PREDICTION_FORMULA

    return {
        "date": day.isoformat(),
        "pool_dates": scan["pool_dates"],
        "disclaimer": BACKTEST_DISCLAIMER,
        "formula": PREDICTION_FORMULA,
        "baseline_rate": history.get("baseline_rate"),
        "bands": history["bands"],
        "items": items,
    }
