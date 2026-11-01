"""把信号落成不可变事件（signal_id + 快照指纹）。只增不删，验证在事件表上重放。"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from backend.app.models.tables import SignalEvent, SignalSnapshot


def _code_id(prefix: str, day: date, code: str, i: int) -> str:
    raw = f"{prefix}:{day.isoformat()}:{code}:{i}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix[:12]}-{day.isoformat()}-{digest}"


def _fingerprint(day: date, code: str, features: dict) -> str:
    raw = json.dumps({"day": day.isoformat(), "code": code, "features": features}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_signals(
    session,
    signal_type: str,
    day: date,
    items: list[dict],
    *,
    model: str = "",
    prompt_version: str = "",
    temperature: float | None = None,
) -> int:
    """把一组信号写成 SignalEvent。返回新写入的条数（同 fingerprint 不重复写）。"""
    count = 0
    for i, item in enumerate(items):
        code = str(item.get("code") or "")
        if not code:
            continue
        fingerprint = _fingerprint(day, code, item.get("features") or {})
        exists = (
            session.query(SignalEvent.id)
            .filter(
                SignalEvent.signal_type == signal_type,
                SignalEvent.signal_date == day,
                SignalEvent.stock_code == code,
                SignalEvent.snapshot_fingerprint == fingerprint,
            )
            .first()
        )
        if exists:
            continue
        signal_id = _code_id(signal_type, day, code, i)
        session.add(
            SignalEvent(
                signal_id=signal_id,
                signal_date=day,
                signal_type=signal_type,
                stock_code=code,
                confidence=item.get("confidence"),
                model=model,
                prompt_version=prompt_version,
                temperature=temperature,
                snapshot_fingerprint=fingerprint,
                payload={"features": item.get("features") or {}, "reason": item.get("reason") or ""},
            )
        )
        count += 1
    session.commit()
    return count


def snapshot_fingerprint(session, day: date) -> str | None:
    row = (
        session.query(SignalSnapshot)
        .filter(SignalSnapshot.trade_date == day)
        .order_by(SignalSnapshot.id.desc())
        .first()
    )
    return row.fingerprint if row is not None else None
