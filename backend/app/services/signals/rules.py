"""规则信号：基于已有的相似度 / 涨停 / 量能特征，纯本地计算，不调 AI。

每个规则产出信号列表，供事件研究验证。规则信号是「可验证」的基准之一：
与 AI 信号分开记录（signal_type 不同），验证时各自独立。
"""

from __future__ import annotations

from datetime import date

from backend.app.models.tables import DailyQuote
from backend.app.providers.markets import is_in_scope


def rule_high_similarity(session, day: date, *, top: int = 20, threshold: float = 90.0) -> list[dict]:
    """高度相似信号：全市场按最高相似度排序，取 >= threshold 的前 top 只。"""
    from backend.app.services.similarity_service import scan_market

    payload = scan_market(session, day, max(50, top * 2), 0)
    items = [
        item
        for item in payload["items"]
        if is_in_scope(item["code"]) and (item.get("max_similarity") or 0) >= threshold
    ]
    items.sort(key=lambda x: -(x.get("max_similarity") or 0))
    return [
        {
            "code": item["code"],
            "name": item.get("name") or "",
            "confidence": float(item.get("max_similarity") or 0),
            "features": {
                "max_similarity": item.get("max_similarity"),
                "avg_similarity": item.get("avg_similarity"),
                "high_similarity_count": item.get("high_similarity_count"),
                "band": item.get("band"),
                "is_limit_up": item.get("is_limit_up"),
            },
        }
        for item in items[:top]
    ]


def rule_volume_surge(session, day: date, *, top: int = 20, min_ratio: float = 1.3) -> list[dict]:
    """放量上攻信号：当日成交量 >= 前一日 1.3 倍，且当日上涨。"""
    rows = (
        session.query(DailyQuote)
        .filter(DailyQuote.trade_date == day)
        .order_by(DailyQuote.stock_code)
        .all()
    )
    prev = {
        row.stock_code: row
        for row in session.query(DailyQuote)
        .filter(DailyQuote.trade_date < day)
        .order_by(DailyQuote.trade_date.desc())
        .all()
    }
    seen = set()
    signals = []
    for row in rows:
        if row.stock_code in seen or not is_in_scope(row.stock_code):
            continue
        seen.add(row.stock_code)
        before = prev.get(row.stock_code)
        if before is None or not before.volume or not row.volume:
            continue
        ratio = row.volume / before.volume
        if ratio < min_ratio or (row.change_percent or 0) <= 0:
            continue
        signals.append(
            {
                "code": row.stock_code,
                "name": "",
                "confidence": round(min(100.0, 50.0 + ratio * 20.0), 1),
                "features": {"volume_ratio": round(ratio, 2), "change_percent": row.change_percent},
            }
        )
    signals.sort(key=lambda x: -x["features"]["volume_ratio"])
    return signals[:top]


RULES = {
    "high_similarity": rule_high_similarity,
    "volume_surge": rule_volume_surge,
}


def generate_rules(session, day: date, *, names: list[str] | None = None) -> dict[str, list[dict]]:
    """一次跑全部（或指定）规则信号。"""
    wanted = names or list(RULES)
    return {name: RULES[name](session, day) for name in wanted if name in RULES}
