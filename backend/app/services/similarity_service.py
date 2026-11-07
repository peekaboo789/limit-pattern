"""形态相似度扫描，按旧版《前 100 行是怎么挑出来的》实现。

样本池：近 N 个交易日（设置，默认 30）的全部涨停样本，每个样本的向量只含它涨停日之前的三天。
候选：评估日在库的全市场（含当天已涨停的股票），窗口是截至评估日的三天。
每只候选去掉自己的全部样本后，取四个数：比对样本数、最高相似度、平均相似度、高度相似数量。
前 N 名按 (−最高相似度, −最高相似度, 代码升序) 排。
"""

from __future__ import annotations

from datetime import date

import numpy as np

from backend.app.algorithms.features import Bar, build_three_day_window
from backend.app.algorithms.similarity import SimilarityEngine, describe_band, summarize
from backend.app.models.tables import DailyQuote, Stock
from backend.app.providers.markets import BOARD_LABELS, classify, is_in_scope
from backend.app.services.history_backfill import PARTIAL_SOURCES, bulk_bars, ensure_prior_bars, load_bars
from backend.app.services.retention import quote_dates

DISCLAIMER = "相似度不是涨停概率"
SCORE_FEATURES = ("position_10", "position_20", "position_60", "body_percent", "close_change", "volume_ratio_5", "volume_ratio_10", "amplitude")
_ENGINES: dict = {}


def pool_dates(session, as_of: date) -> list[date]:
    from backend.app.core.config import sample_pool_days

    days = quote_dates(session, as_of)
    return days[-sample_pool_days():]


def trading_days_before(day: date, count: int, *, inclusive: bool) -> list[date]:
    """按交易日历往前数 count 个交易日（升序）。inclusive=True 时含 day 本身（day 须是交易日）。"""
    from backend.app.core.calendar import is_trading_day, previous_trading_day

    out: list[date] = []
    cursor = day if inclusive and is_trading_day(day) else previous_trading_day(day)
    while len(out) < count:
        out.append(cursor)
        cursor = previous_trading_day(cursor)
    return sorted(out)


class _Listing:
    """「上市满 20 个完整交易日」：有上市日期就按日历数；没有就数库里的完整日线；
    库里日线最早一天就是全库最早一天（历史被保留天数截断，不是新股）时视为已满 20 天。"""

    def __init__(self, session) -> None:
        stored = quote_dates(session)
        self.earliest = stored[0] if stored else None
        self.list_dates = {code: d for code, d in session.query(Stock.code, Stock.list_date) if d is not None}

    def days(self, code: str, bars: list[Bar], end: date) -> int | None:
        listed = self.list_dates.get(code)
        if listed is not None:
            from backend.app.core.calendar import is_trading_day

            count, cursor = 0, listed
            from datetime import timedelta

            while cursor <= end and count < 40:
                count += is_trading_day(cursor)
                cursor += timedelta(days=1)
            return count
        full = [bar for bar in bars if bar.trade_date <= end and not bar.is_partial]
        if len(full) < 20 and full and self.earliest is not None and full[0].trade_date <= self.earliest:
            return 20
        return None


def _window(session, listing: _Listing, code: str, day: date, *, include: bool):
    bars = load_bars(session, code, day)
    expected = trading_days_before(day, 3, inclusive=include)
    end = expected[-1]
    return build_three_day_window(
        bars, day, include_as_of_date=include, expected_days=expected, listed_days=listing.days(code, bars, end)
    )


def _samples(session, as_of: date) -> tuple[list[date], list[dict], list[dict]]:
    dates = pool_dates(session, as_of)
    if not dates:
        return [], [], []
    names = _names(session)
    listing = _Listing(session)
    rows = (
        session.query(DailyQuote)
        .filter(DailyQuote.trade_date.in_(dates), DailyQuote.is_limit_up.is_(True))
        .all()
    )
    samples: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        if not is_in_scope(row.stock_code) or (row.source or "") in ("intraday", "morning_freeze", "close"):  # 临时日线（含收盘报价快照）的涨停都不进样本池
            continue  # 盘中临时线上的「涨停」还没收盘，不进样本池
        reason = ensure_prior_bars(session, row.stock_code, row.trade_date, as_of)
        window = _window(session, listing, row.stock_code, min(row.trade_date, as_of), include=False) if row.trade_date <= as_of else None
        if window is None or not window.ok:
            skipped.append(
                {
                    "code": row.stock_code,
                    "name": names.get(row.stock_code, ""),
                    "limit_date": row.trade_date.isoformat(),
                    "reason": reason or {
                        "insufficient_trading_days": "涨停前不足 3 个交易日",
                        "listed_less_than_20_days": "上市不足 20 个完整交易日",
                        "invalid_bar_in_window": "涨停前三天有缺行情/停牌/零成交",
                    }.get(window.reason if window else "", "凑不齐涨停前三根K线"),
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
    # 每个样本「次日是否继续涨停」：次日晚于评估日或库里没有 → None（未定判，不进分母），严格不看未来
    from backend.app.core.calendar import next_trading_day

    stored = set(quote_dates(session, as_of))
    nexts = {next_trading_day(s["limit_date"]) for s in samples}
    hits = {
        (code, day)
        for code, day in session.query(DailyQuote.stock_code, DailyQuote.trade_date).filter(
            DailyQuote.trade_date.in_([d for d in nexts if d in stored]), DailyQuote.is_limit_up.is_(True)
        )
    }
    for sample in samples:
        nxt = next_trading_day(sample["limit_date"])
        sample["next_limit_up"] = None if nxt > as_of or nxt not in stored else (sample["code"], nxt) in hits
    samples.sort(key=lambda item: (item["limit_date"], item["code"]))
    skipped.sort(key=lambda item: (item["limit_date"], item["code"]))
    return dates, samples, skipped


def _engine(as_of: date, samples: list[dict]) -> SimilarityEngine:
    from backend.app.core.config import similarity_weights

    eu, co = similarity_weights()
    key = (as_of, len(samples), samples[0]["code"], samples[-1]["code"], eu, co, round(float(np.nansum(samples[-1]["vector"])), 6))
    cached = _ENGINES.get("key")
    if cached == key:
        return _ENGINES["engine"]
    engine = SimilarityEngine(eu, co).fit(np.asarray([s["vector"] for s in samples], dtype=float))
    _ENGINES.update(key=key, engine=engine)
    return engine


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
    with bulk_bars(session, as_of):
        return _similar_to(session, code, as_of, top)


def _similar_to(session, code: str, as_of: date, top: int) -> dict:
    from backend.app.core.config import similarity_threshold_high

    names = _names(session)
    dates, samples, skipped = _samples(session, as_of)
    window = _window(session, _Listing(session), code, as_of, include=True)
    if not window.ok:
        raise LookupError(
            {
                "insufficient_trading_days": "这只股票在这一天不足 3 个交易日",
                "listed_less_than_20_days": "这只股票上市不足 20 个完整交易日，不参与比对",
                "invalid_bar_in_window": "这只股票近三天有缺行情/停牌/零成交",
            }.get(window.reason, "这只股票在这一天凑不齐三天行情")
        )
    target_window = [bar.trade_date.isoformat() for bar in window.days]
    payload = _empty(code, names.get(code, ""), dates, target_window)
    payload["pool_size"] = len(samples)
    payload["skipped"] = skipped
    keep = [i for i, sample in enumerate(samples) if sample["code"] != code]
    if not keep:
        return payload
    engine = _engine(as_of, samples)
    total, euclid, cosine = engine.score_z(engine.standardize(np.asarray(window.vector, dtype=float).reshape(1, -1)))
    row = total[0][keep]
    eu_row, co_row = euclid[0][keep], cosine[0][keep]
    stats = summarize(row, similarity_threshold_high())
    payload["stats"] = {k: v for k, v in stats.items() if k != "best"}
    payload["stats"]["band"] = describe_band(stats["max_similarity"])
    order = sorted(range(len(keep)), key=lambda j: (-float(row[j]), samples[keep[j]]["code"]))
    matches = []
    for j in order[:top]:
        sample = samples[keep[j]]
        matches.append(
            {
                "code": sample["code"],
                "name": names.get(sample["code"], ""),
                "limit_date": sample["limit_date"].isoformat(),
                "score": _round(row[j]),
                "euclidean_score": _round(eu_row[j]),
                "cosine_score": _round(co_row[j]),
                "band": describe_band(float(row[j])),
                "group_scores": engine.group_scores(np.asarray(window.vector), np.asarray(sample["vector"])),
                "window": sample["window"],
            }
        )
    payload["matches"] = matches
    return payload


def scan_market(session, as_of: date, top: int, min_score: float, details: bool = True) -> dict:
    with bulk_bars(session, as_of):
        return _scan_market(session, as_of, top, min_score, details)


def _scan_market(session, as_of: date, top: int, min_score: float, details: bool = True) -> dict:
    from backend.app.core.config import similarity_threshold_high

    threshold = similarity_threshold_high()
    dates, samples, skipped = _samples(session, as_of)
    names = _names(session)
    base = {
        "date": as_of.isoformat(),
        "pool_dates": [day.isoformat() for day in dates],
        "pool_size": len(samples),
        "disclaimer": DISCLAIMER,
        "rank_key": "(−最高相似度, −最高相似度, 代码升序)",
        "items": [],
        "skipped": skipped,
        "target_skips": {},
    }
    if not samples:
        return base
    today_rows = {
        row.stock_code: row
        for row in session.query(DailyQuote).filter(DailyQuote.trade_date == as_of)
        if is_in_scope(row.stock_code)
    }
    listing = _Listing(session)
    targets: list[tuple[str, list[float], dict]] = []
    skips: dict[str, int] = {}
    for code in sorted(today_rows):
        window = _window(session, listing, code, as_of, include=True)
        if window.ok:
            targets.append((code, window.vector, window.day_features[-1]))
        else:
            skips[window.reason] = skips.get(window.reason, 0) + 1
    base["target_skips"] = skips
    if not targets:
        return base
    engine = _engine(as_of, samples)
    sample_codes = np.asarray([sample["code"] for sample in samples])
    outcome = np.asarray([np.nan if s["next_limit_up"] is None else float(s["next_limit_up"]) for s in samples])
    from backend.app.services.score_model import EVIDENCE_MATCH_LIMIT, weighted_carry_rate

    items = []
    chunk = 600
    for start in range(0, len(targets), chunk):
        block = targets[start : start + chunk]
        scores = engine.score(np.asarray([vector for _c, vector, _f in block], dtype=float))
        for r, (code, vector, feats) in enumerate(block):
            mask = sample_codes != code  # 同一代码的所有样本都去掉
            row = scores[r][mask]
            stats = summarize(row, threshold)
            if stats["max_similarity"] is None or stats["max_similarity"] < min_score:
                continue
            idx = np.flatnonzero(mask)
            best = samples[int(idx[stats["best"]])]
            order = np.argsort(-row, kind="stable")
            top3 = order[:3]
            out_row = outcome[idx]
            settled_mask = ~np.isnan(out_row)
            near = order[settled_mask[order]][:EVIDENCE_MATCH_LIMIT]
            evidence = [(float(row[j]), bool(out_row[j])) for j in near]
            high_mask = settled_mask & (row >= threshold)
            n_high = int(high_mask.sum())
            history_rate = round(float(out_row[high_mask].mean()) * 100, 2) if n_high else None
            quote = today_rows[code]
            items.append(
                {
                    "code": code,
                    "name": names.get(code, ""),
                    "score": stats["max_similarity"],
                    "max_similarity": stats["max_similarity"],
                    "avg_similarity": stats["avg_similarity"],
                    "sample_count": stats["sample_count"],
                    "high_similarity_count": stats["high_similarity_count"],
                    "band": describe_band(stats["max_similarity"]),
                    "top_sample": best["code"],
                    "is_limit_up": bool(quote.is_limit_up),
                    "change_percent": quote.change_percent if quote.change_percent is not None else feats.get("close_change"),
                    "best_match": {"code": best["code"], "name": names.get(best["code"], ""), "limit_date": best["limit_date"].isoformat()},
                    "top_matches": [
                        {
                            "code": samples[int(idx[j])]["code"],
                            "limit_date": samples[int(idx[j])]["limit_date"].isoformat(),
                            "score": _round(row[j]),
                        }
                        for j in top3
                    ],
                    "carry_rate": weighted_carry_rate(evidence),
                    "history_limit_up_rate": history_rate,
                    "history_settled": n_high,
                    "features": {
                        k: (None if feats.get(k) is None or feats.get(k) != feats.get(k) else round(float(feats[k]), 4))
                        for k in SCORE_FEATURES
                    },
                    "_vector": vector,
                }
            )
    items.sort(key=lambda item: (-item["max_similarity"], -item["max_similarity"], item["code"]))
    chosen = items[:top]
    for item in items[top:]:
        item.pop("_vector", None)
    for item in chosen:
        vector = item.pop("_vector")
        if not details:
            continue
        sample = next(s for s in samples if s["code"] == item["best_match"]["code"] and s["limit_date"].isoformat() == item["best_match"]["limit_date"])
        item["group_scores"] = engine.group_scores(np.asarray(vector), np.asarray(sample["vector"]))
    base["items"] = chosen
    return base


def feed_order(items: list[dict]) -> list[dict]:
    """喂给模型的顺序：已涨停段在前、未涨停段在后，各段内仍按最高相似度降序（不是全局排序）。"""
    return [item for item in items if item.get("is_limit_up")] + [item for item in items if not item.get("is_limit_up")]


def board_name(code: str) -> str:
    _exchange, board = classify(code)
    return BOARD_LABELS.get(board or "", "未知")
