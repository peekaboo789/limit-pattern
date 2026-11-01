"""上线闭环的推送门：复用现有单一报警门（monitor_service.alert_phase）+ 去重（client.once）+ 冷却。

只在交易时段报警、有冷却、有去重，且信号处于可推送状态（shadow.py 的 can_alert）。
"""

from __future__ import annotations

import time as _time
from datetime import datetime

from backend.app.core.clock import now
from backend.app.services.monitor_service import alert_phase

COOLDOWN_SECONDS = 1800  # 同一信号类型半小时内不再重复推送
_cooldown: dict[str, float] = {}


def _key(signal_type: str, code: str) -> str:
    return f"{signal_type}:{code}"


def in_cooldown(signal_type: str, code: str, moment: datetime | None = None) -> bool:
    moment = moment or now()
    key = _key(signal_type, code)
    last = _cooldown.get(key)
    if last is None:
        return False
    return (_time.monotonic() - last) < COOLDOWN_SECONDS


def mark(signal_type: str, code: str) -> None:
    _cooldown[_key(signal_type, code)] = _time.monotonic()


def gated(session, signal_type: str, code: str, *, moment: datetime | None = None) -> dict:
    """返回该信号此刻是否允许推送，以及原因。复用现有报警门。"""
    from backend.app.services.signals import shadow

    moment = moment or now()
    phase = alert_phase(moment)
    if not phase["alerts_active"]:
        return {"allowed": False, "reason": phase["phase"], "phase": phase}
    if not shadow.can_alert(session, signal_type):
        return {"allowed": False, "reason": "shadow", "phase": phase}
    if in_cooldown(signal_type, code, moment):
        return {"allowed": False, "reason": "cooldown", "phase": phase}
    # 去重交给调用方用 client.once；这里只负责时段门 + 冷却 + 影子状态
    return {"allowed": True, "reason": "", "phase": phase}
