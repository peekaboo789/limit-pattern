"""多模型讨论验证 API。前缀 /api/council。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Path, Query

router = APIRouter()


def _parse_day(value: str | None, default_latest: bool) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
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


@router.post("/reconcile-stale")
def council_reconcile_stale() -> dict:
    from backend.app.services.council import service

    return {"terminated": service.reconcile_stale_sessions()}


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


@router.get("/models")
def council_models() -> dict:
    from backend.app.services.council import service

    return {"items": service.council_models()}


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


@router.get("/{session_id}")
def council_get(session_id: int) -> dict:
    from backend.app.services.council import service

    row = service.get(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这场讨论")
    return row


@router.post("/{session_id}/user-message")
def council_user_message(session_id: int = Path(...), body: dict | None = None) -> dict:
    from backend.app.services.council import service

    text = (body or {}).get("text")
    try:
        return service.add_user_message(session_id, text)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{session_id}/speak")
def council_speak(session_id: int = Path(...), body: dict | None = None) -> dict:
    from backend.app.services.council import service

    profile_id = (body or {}).get("profile_id")
    round_no = (body or {}).get("round")
    try:
        return service.speak(session_id, profile_id, round_no)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
