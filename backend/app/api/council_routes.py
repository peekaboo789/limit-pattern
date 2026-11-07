"""多模型讨论验证 API。前缀 /api/council。"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()


class CouncilBatchPreviewItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: int | None = None
    code: str
    name: str = ""
    market: str | None = None
    board: str | None = None
    board_label: str | None = None
    selected: bool = True


class CouncilBatchPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_version: int | None = None
    source_name: str | None = None
    source_date: str | None = None
    default_eval_date: str | None = None
    counts: dict | None = None
    errors: list[dict] | None = None
    items: list[CouncilBatchPreviewItemIn] = Field(default_factory=list)


class CouncilBatchStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview: CouncilBatchPreviewIn
    eval_date: str
    profile_ids: list[str]
    source_name: str = ""
    idempotency_key: str | None = None


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


@router.post("/batch/preview")
async def council_batch_preview(request: Request) -> dict:
    from backend.app.services.council import batch_service

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > batch_service.MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"文件超过 {batch_service.MAX_UPLOAD_BYTES} 字节上限")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > batch_service.MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"文件超过 {batch_service.MAX_UPLOAD_BYTES} 字节上限")
        chunks.append(chunk)
    source_name = request.headers.get("x-file-name", "")
    try:
        return batch_service.preview_upload(b"".join(chunks), source_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/batch/start")
def council_batch_start(body: CouncilBatchStartIn) -> dict:
    from backend.app.services.council import batch_service

    try:
        return batch_service.create_batch(
            body.preview.model_dump(),
            body.eval_date,
            body.profile_ids,
            body.source_name,
            body.idempotency_key,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/batch")
def council_batch_list(limit: int = Query(50, ge=1, le=200)) -> dict:
    from backend.app.services.council import batch_service

    return {"items": batch_service.list_batches(limit=limit)}


@router.get("/batch/{batch_id}")
def council_batch_get(batch_id: int) -> dict:
    from backend.app.services.council import batch_service

    row = batch_service.get_batch(batch_id)
    if row is None:
        raise HTTPException(status_code=404, detail="没有这个批次")
    return row


@router.post("/batch/{batch_id}/cancel")
def council_batch_cancel(batch_id: int) -> dict:
    from backend.app.services.council import batch_service

    try:
        return batch_service.cancel_batch(batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/batch/{batch_id}/retry-failed")
def council_batch_retry_failed(batch_id: int) -> dict:
    from backend.app.services.council import batch_service

    try:
        return batch_service.retry_failed(batch_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.get("/sources")
def council_sources():
    from backend.app.services.council.source_registry import get_registry
    return get_registry().view()


@router.put("/sources/{source_id}")
def council_configure_source(source_id: str, body: dict):
    from backend.app.services.council.source_registry import get_registry
    try:
        return get_registry().configure(source_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _source_action(action):
    from backend.app.services.council.source_registry import get_registry
    try:
        return getattr(get_registry(), action)()
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"来源清单请求失败（保留当前配置）：{type(exc).__name__}") from exc


@router.post("/sources/check-update")
def council_sources_check():
    return _source_action("check_update")


@router.post("/sources/apply-update")
def council_sources_apply():
    return _source_action("apply_update")


@router.post("/sources/rollback")
def council_sources_rollback():
    return _source_action("rollback")


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


@router.delete("/{session_id}")
def council_delete(session_id: int = Path(..., ge=1)) -> dict:
    from backend.app.services.council import service
    try:
        return service.delete_session(session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
