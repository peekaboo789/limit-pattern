"""信号模块接口：有效性报告、事件列表、影子状态、重放说明、生成信号。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from backend.app.core.database import session_scope
from backend.app.services.signals import report as signal_report
from backend.app.services.signals import shadow

router = APIRouter()


def _signal_type_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


@router.get("/signals/report")
def get_signal_report(signal_type: str | None = None, settle: bool = Query(True)) -> dict:
    """有效性报告：按信号类型产出 T+1~T+5 超额收益、显著性、校准、分市场状态。"""
    with session_scope() as session:
        return signal_report.build_report(session, _signal_type_or_none(signal_type), settle=settle)


@router.get("/signals/types")
def get_signal_types() -> dict:
    with session_scope() as session:
        return {"items": signal_report.list_signal_types(session)}


@router.get("/signals/shadow")
def get_shadow_status() -> dict:
    with session_scope() as session:
        return {"items": shadow.all_statuses(session)}


@router.post("/signals/generate")
def post_signals_generate(body: dict | None = None) -> dict:
    """生成当日规则信号并落为事件（影子模式，只记录不提醒）。"""
    from backend.app.core.clock import today
    from backend.app.core.database import session_scope as _scope
    from backend.app.services.retention import quote_dates
    from backend.app.services.signals import rules
    from backend.app.services.signals.record import record_signals

    body = body or {}
    day = body.get("date")
    from backend.app.api.routes import _parse_day

    if day:
        signal_day = _parse_day(day, default_latest=False)
    else:
        signal_day = today()
    with _scope() as session:
        days = quote_dates(session)
        if signal_day not in days:
            raise HTTPException(status_code=404, detail="这一天没有行情")
        generated = rules.generate_rules(session, signal_day)
        counts = {}
        for sig_type, items in generated.items():
            counts[sig_type] = record_signals(session, sig_type, signal_day, items)
        return {"date": signal_day.isoformat(), "counts": counts}
