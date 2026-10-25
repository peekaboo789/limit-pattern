"""只和对比窗口（设置里的最近若干个交易日，默认 3）内的涨停样本比。样本窗口不含涨停当天，候选窗口含评估日。"""

from __future__ import annotations

from datetime import date

import numpy as np

from backend.app.algorithms.features import build_three_day_window
from backend.app.algorithms.similarity import percent, score_components
from backend.app.core.config import compare_window_days
from backend.app.models.tables import DailyQuote, Stock
from backend.app.providers.markets import BOARD_LABELS, classify, is_in_scope
from backend.app.services.history_backfill import ensure_prior_bars, load_bars
from backend.app.services.retention import quote_dates

DISCLAIMER = "相似度不是涨停概率"


def pool_dates(session, as_of: date) -> list[date]:
    """样本池覆盖的交易日：`as_of` 及往前，共「对比窗口」那么多个交易日。全工程只算这一份。"""
    days = quote_dates(session, as_of)
    return days[-compare_window_days():]


def limit_up_samples(session, as_of: date) -> tuple[list[date], list[dict], list[dict]]:
    """截至 `as_of`（含）、对比窗口内的涨停样本。盘中要用它时必须传收盘日，别传当天。"""
    dates = pool_dates(session, as_of)
    if not dates:
        return [], [], []
    names = _names(session)
    rows = (
        session.query(DailyQuote)
        .filter(DailyQuote.trade_date.in_(dates), DailyQuote.is_limit_up.is_(True))
        .all()
    )
    samples: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        if not is_in_scope(row.stock_code):
            continue
        reason = ensure_prior_bars(session, row.stock_code, row.trade_date, as_of)
        window = build_three_day_window(
            load_bars(session, row.stock_code, min(row.trade_date, as_of)),
            row.trade_date,
            include_as_of_date=False,
        )
        if not window.ok:
            skipped.append(
                {
                    "code": row.stock_code,
                    "name": names.get(row.stock_code, ""),
                    "limit_date": row.trade_date.isoformat(),
                    "reason": reason or "凑不齐涨停前三根K线",
                }
            )
            continue
        samples.append(
            {
                "code": row.stock_code,
                "limit_date": row.trade_date,
                "vector": window.vector,
                "window": [bar.trade_date.isoformat() for bar in window.days],
            }
        )
    samples.sort(key=lambda item: (item["limit_date"], item["code"]))
    skipped.sort(key=lambda item: (item["limit_date"], item["code"]))
    return dates, samples, skipped


def _names(session) -> dict[str, str]:
    return {row.code: row.name for row in session.query(Stock.code, Stock.name)}


def _round(value: float) -> float:
    return round(float(value), 2)


def _empty(code: str, name: str, dates: list[date], window: list[str] | None = None) -> dict:
    return {
        "target": {"code": code, "name": name, "window": window or []},
        "pool_dates": [day.isoformat() for day in dates],
        "pool_size": 0,
        "disclaimer": DISCLAIMER,
        "matches": [],
        "skipped": [],
    }


def similar_to(session, code: str, as_of: date, top: int) -> dict:
    """单只股票对整池的相似度。

    尺子按**整个样本池**拟合，剔自身只剔分数、不剔拟合——和首页榜、盘中榜同一条路。
    反过来（先把自身样本剔掉再拟合）会让同一只票在同一天出现两个数：
    榜上 26.3、详情页 20.53。那不是「更精确」，那是两把尺子。
    """
    names = _names(session)
    dates, samples, skipped = limit_up_samples(session, as_of)
    window = build_three_day_window(load_bars(session, code, as_of), as_of, include_as_of_date=True)
    if not window.ok:
        raise LookupError("这只股票在这一天凑不齐三天行情")
    target_window = [bar.trade_date.isoformat() for bar in window.days]
    payload = _empty(code, names.get(code, ""), dates, target_window)
    payload["pool_size"] = len(samples)
    payload["skipped"] = skipped
    if not samples:
        return payload
    parts = score_components(
        np.asarray(window.vector, dtype=float),
        np.asarray([sample["vector"] for sample in samples], dtype=float),
    )
    scores = parts["score"][0].astype(float).copy()
    for index, sample in enumerate(samples):
        if sample["code"] == code:
            scores[index] = -1.0
    order = [
        index
        for index in np.argsort(-scores)
        if float(scores[index]) >= 0.0
    ]
    matches = []
    for index in order[:top]:
        sample = samples[int(index)]
        matches.append(
            {
                "code": sample["code"],
                "name": names.get(sample["code"], ""),
                "limit_date": sample["limit_date"].isoformat(),
                "score": _round(parts["score"][0, index]),
                "shape_score": _round(parts["shape"][0, index]),
                "volume_score": _round(parts["volume"][0, index]),
                "volume_pct_score": _round(parts["volume_pct"][0, index]),
                "other_scores": {"amount": percent(parts["amount"][0, index])},
                "window": sample["window"],
            }
        )
    payload["matches"] = matches
    return payload


def scan_market(session, as_of: date, top: int, min_score: float) -> dict:
    dates, samples, skipped = limit_up_samples(session, as_of)
    names = _names(session)
    base = {
        "date": as_of.isoformat(),
        "pool_dates": [day.isoformat() for day in dates],
        "pool_size": len(samples),
        "disclaimer": DISCLAIMER,
        "items": [],
        "skipped": skipped,
    }
    if not samples:
        return base
    codes = [
        row[0]
        for row in session.query(DailyQuote.stock_code)
        .filter(DailyQuote.trade_date == as_of)
        .distinct()
    ]
    targets: list[tuple[str, list[float]]] = []
    for code in codes:
        if not is_in_scope(code):
            continue
        window = build_three_day_window(load_bars(session, code, as_of), as_of, include_as_of_date=True)
        if window.ok:
            targets.append((code, window.vector))
    if not targets:
        return base
    parts = score_components(
        np.asarray([vector for _code, vector in targets], dtype=float),
        np.asarray([sample["vector"] for sample in samples], dtype=float),
    )
    items = []
    for row_index, (code, _vector) in enumerate(targets):
        scores = parts["score"][row_index].astype(float).copy()
        for sample_index, sample in enumerate(samples):
            if sample["code"] == code:
                scores[sample_index] = -1.0
        if scores.size == 0 or float(scores.max()) < 0:
            continue
        best = int(np.argmax(scores))
        score = float(scores[best])
        if score < min_score:
            continue
        sample = samples[best]
        items.append(
            {
                "code": code,
                "name": names.get(code, ""),
                "score": _round(score),
                "best_match": {
                    "code": sample["code"],
                    "name": names.get(sample["code"], ""),
                    "limit_date": sample["limit_date"].isoformat(),
                },
                "shape_score": _round(parts["shape"][row_index, best]),
                "volume_score": _round(parts["volume"][row_index, best]),
                "volume_pct_score": _round(parts["volume_pct"][row_index, best]),
            }
        )
    items.sort(key=lambda item: (-item["score"], item["code"]))
    base["items"] = items[:top]
    return base


def board_name(code: str) -> str:
    _exchange, board = classify(code)
    return BOARD_LABELS.get(board or "", "未知")
