"""信号日快照：未复权日线 + 涨跌停价 + 停牌 + ST + 情绪分组。存 DB + JSON 文件，验证只读快照。

关键原则：信号日 15:00 后先固化一份不可变快照（fingerprint = 内容哈希），验证永远在快照上重放。
只取「信号日及以前」的数据，杜绝未来函数。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from backend.app.core.config import data_dir
from backend.app.models.tables import DailyQuote, SignalSnapshot, Stock
from backend.app.providers.markets import is_in_scope
from backend.app.services.ai_context import market_context


def snapshots_dir():
    path = data_dir() / "signal_snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint_of(payload: dict) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def build_snapshot(session, day: date) -> dict:
    """信号日全市场快照：每只股票一行（未复权 OHLCV + 涨跌停价 + 停牌/ST 标记），加情绪分组。

    只用 <= day 的行情，不含任何未来数据。
    """
    rows = session.query(DailyQuote).filter(DailyQuote.trade_date == day).all()
    stocks = {row.code: row for row in session.query(Stock)}
    quotes = []
    for row in rows:
        if not is_in_scope(row.stock_code):
            continue
        stock = stocks.get(row.stock_code)
        suspended = not row.volume or row.volume <= 0
        quotes.append(
            {
                "code": row.stock_code,
                "name": stock.name if stock else "",
                "board": stock.board if stock else "",
                "is_st": bool(stock.is_st) if stock else False,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "pre_close": row.pre_close,
                "volume": row.volume,
                "amount": row.amount,
                "turnover_rate": row.turnover_rate,
                "change_percent": row.change_percent,
                "is_limit_up": bool(row.is_limit_up),
                "is_limit_down": bool(row.is_limit_down),
                "limit_up_price": row.limit_up_price,
                "limit_down_price": row.limit_down_price,
                "suspended": bool(suspended),
            }
        )
    market = market_context(session, day)
    return {
        "trade_date": day.isoformat(),
        "quotes": quotes,
        "market": market,
    }


def save_snapshot(session, day: date) -> dict:
    """固化快照到 DB + JSON 文件，返回带 fingerprint 的快照。幂等：同指纹不重复写。"""
    payload = build_snapshot(session, day)
    fingerprint = fingerprint_of(payload)
    existing = (
        session.query(SignalSnapshot)
        .filter(SignalSnapshot.trade_date == day, SignalSnapshot.fingerprint == fingerprint)
        .first()
    )
    if existing is None:
        session.add(SignalSnapshot(trade_date=day, fingerprint=fingerprint, payload=payload))
        session.commit()
    path = snapshots_dir() / f"{day.isoformat()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"trade_date": day.isoformat(), "fingerprint": fingerprint, "path": str(path)}


def load_snapshot(session, day: date) -> dict | None:
    """优先读 DB；文件没有再从 DB 补。返回快照 payload 或 None。"""
    row = (
        session.query(SignalSnapshot)
        .filter(SignalSnapshot.trade_date == day)
        .order_by(SignalSnapshot.id.desc())
        .first()
    )
    if row is not None:
        return dict(row.payload or {})
    return None


def snapshot_bar(payload: dict, code: str) -> dict | None:
    """从快照里取某只股票的信号日 bar。"""
    for bar in payload.get("quotes") or []:
        if bar.get("code") == code:
            return dict(bar)
    return None
