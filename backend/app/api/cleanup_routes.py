"""数据清理接口：只做后端 + 网页，不接入企业微信/个人微信命令。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.app.services import data_cleanup

router = APIRouter()


@router.get("/data-cleanup/preview")
def data_cleanup_preview(days: int = 60, mode: str = "manual") -> dict:
    try:
        if mode not in {"manual", "auto"}:
            raise ValueError("mode 只能是 manual 或 auto")
        return data_cleanup.preview(days, mode=mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/data-cleanup/execute")
def data_cleanup_execute(body: dict | None = None) -> dict:
    body = body or {}
    if body.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="危险操作需要二次确认（confirm: true）")
    days = body.get("days") or None
    mode = body.get("mode") or "manual"
    if mode not in {"manual", "auto"}:
        raise HTTPException(status_code=422, detail="mode 只能是 manual 或 auto")
    try:
        return data_cleanup.execute(int(days) if days is not None else None, mode=mode)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 备份失败、磁盘失败等对用户可见
        raise HTTPException(status_code=500, detail=f"删除未完成：{exc}") from exc
