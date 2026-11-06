"""多模型讨论验证 API。前缀 /api/council。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()


def _parse_day(value: str | None, default_latest: bool) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from exc


@router.post("/run")
def council_run(body: dict) -> dict:
    from backend.app.services.council import service

    body = body or {}
    day = _parse_day(body.get("date"), True)
    profile_ids = body.get("profile_ids")
    if profile_ids is not None and (not isinstance(profile_ids, list) or any(not isinstance(x, str) for x in profile_ids)):
        raise HTTPException(status_code=422, detail="profile_ids 要是字符串数组")
    try:
        return service.start(str(body.get("code") or body.get("q") or ""), day, profile_ids)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/progress")
def council_progress() -> dict:
    from backend.app.services.council import service

    return service.progress()


@router.get("")
def council_latest() -> dict:
    from backend.app.services.council import service

    row = service.latest()
    if row is None:
        return {"items": []}
    return {"items": [row]}


@router.get("/history")
def council_history(code: str | None = None, date: str | None = None, limit: int = Query(100, ge=1, le=500)) -> dict:
    from backend.app.services.council import service

    day = _parse_day(date, True)
    return {"items": service.history(code=code, day=day, limit=limit)}


@router.get("/{session_id}")
def council_get(session_id: int) -> dict:
    from backend.app.services.council import service

    row = service.get(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这场讨论")
    return row


@router.get("/profile-order")
def council_profile_order() -> dict:
    from backend.app.services.council import service

    return service.profile_order()


@router.put("/profile-order")
def council_profile_order_put(body: dict) -> dict:
    from backend.app.services.council import service

    profile_ids = (body or {}).get("profile_ids")
    if not isinstance(profile_ids, list) or any(not isinstance(x, str) for x in profile_ids):
        raise HTTPException(status_code=422, detail="profile_ids 要是字符串数组")
    try:
        return service.save_profile_order(profile_ids)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
