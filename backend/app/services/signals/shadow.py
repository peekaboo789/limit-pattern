"""影子模式：新信号只记录不提醒，4~8 周达标后才开放推送。

状态机：shadow → passed → live → degraded / disabled。
状态变更只增不改（审计轨迹），阈值是常量、便于测试。
"""

from __future__ import annotations

from datetime import date, datetime

from backend.app.core.database import session_scope
from backend.app.models.tables import SignalShadowStatus

SHADOW_MIN_DAYS = 20  # 4 周（约 20 个交易日）
SHADOW_MAX_DAYS = 40  # 8 周
MIN_EVENTS = 30
MIN_WIN_RATE = 0.5
MIN_EXCESS_MEAN = 0.0


def _now() -> datetime:
    from backend.app.core.clock import now

    return now()


def set_status(signal_type: str, status: str, reason: str = "") -> None:
    with session_scope() as session:
        session.add(SignalShadowStatus(signal_type=signal_type, status=status, reason=reason, changed_at=_now()))
        session.commit()


def current_status(session, signal_type: str) -> str:
    row = (
        session.query(SignalShadowStatus)
        .filter(SignalShadowStatus.signal_type == signal_type)
        .order_by(SignalShadowStatus.id.desc())
        .first()
    )
    return row.status if row is not None else "shadow"


def all_statuses(session) -> dict[str, str]:
    rows = session.query(SignalShadowStatus).order_by(SignalShadowStatus.id).all()
    out: dict[str, str] = {}
    for row in rows:
        out[row.signal_type] = row.status
    return out


def _days_since_first(session, signal_type: str) -> int:
    first = (
        session.query(SignalShadowStatus)
        .filter(SignalShadowStatus.signal_type == signal_type)
        .order_by(SignalShadowStatus.id)
        .first()
    )
    if first is None:
        return 0
    return (_now().date() - first.changed_at.date()).days


def can_alert(session, signal_type: str) -> bool:
    """是否允许推送。影子期只记录不提醒。"""
    status = current_status(session, signal_type)
    if status == "live":
        return True
    if status in ("degraded", "disabled"):
        return False
    # shadow / passed：只有 shadow 期满且达标（passed）才允许
    if status == "passed":
        return True
    return False


def evaluate(session, signal_type: str, *, n_events: int, win_rate: float, excess_mean: float) -> str:
    """按滚动表现决定升/降/停。返回新状态。"""
    status = current_status(session, signal_type)
    days = _days_since_first(session, signal_type)
    if status in ("degraded", "disabled"):
        return status
    if status == "live":
        # 滚动监控：跌破阈值自动降权 / 停用
        if n_events >= MIN_EVENTS and win_rate < MIN_WIN_RATE:
            set_status(signal_type, "degraded", f"滚动胜率 {win_rate:.2%} 低于 {MIN_WIN_RATE:.0%}")
            return "degraded"
        if n_events >= MIN_EVENTS and excess_mean < MIN_EXCESS_MEAN:
            set_status(signal_type, "degraded", f"滚动超额均值 {excess_mean:.2f} 低于 0")
            return "degraded"
        return "live"
    if status == "passed":
        return "passed"
    # shadow：期满且达标 -> passed
    if days >= SHADOW_MIN_DAYS and n_events >= MIN_EVENTS and win_rate >= MIN_WIN_RATE and excess_mean >= MIN_EXCESS_MEAN:
        set_status(signal_type, "passed", f"影子期达标（{days} 天 / {n_events} 事件）")
        return "passed"
    return "shadow"


def shadow_days(session, signal_type: str) -> int:
    return min(_days_since_first(session, signal_type), SHADOW_MAX_DAYS)
