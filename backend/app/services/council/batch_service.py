from __future__ import annotations

import hashlib
import threading
from datetime import date, datetime
from pathlib import Path

from backend.app.core.clock import today
from backend.app.core.database import session_scope
from backend.app.models.tables import AiCouncilBatch, AiCouncilBatchItem, AiCouncilSession
from backend.app.providers.markets import BOARD_LABELS, classify, reject_reason
from backend.app.services.council import service

MAX_UPLOAD_BYTES = 128 * 1024
MAX_LINE_LENGTH = 4096
MAX_CANDIDATES = 100
PREVIEW_VERSION = 1
_BATCH_THREAD: threading.Thread | None = None
_BATCH_STOP = threading.Event()
_BATCH_GUARD = threading.Lock()


def _now() -> datetime:
    return datetime.utcnow()


def _decode_text(payload: bytes) -> str:
    if len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError(f"文件超过 {MAX_UPLOAD_BYTES} 字节上限")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("只支持 UTF-8 / UTF-8 BOM 文本") from exc
    for index, line in enumerate(text.splitlines(), start=1):
        if len(line) > MAX_LINE_LENGTH:
            raise ValueError(f"第 {index} 行超过 {MAX_LINE_LENGTH} 字符上限")
    return text


def _header_map(row: list[str]) -> dict[str, int]:
    return {cell.strip(): index for index, cell in enumerate(row)}


def _split_tsv(line: str) -> list[str]:
    return [cell.strip() for cell in line.rstrip("\n\r").split("\t")]


def preview_text(text: str, source_name: str = "") -> dict:
    rows = text.splitlines()
    if len(rows) < 2:
        raise ValueError("TXT 行数不足，无法识别表头与数据")
    if len(rows) > 1000:
        raise ValueError("TXT 行数超过 1000 行上限")
    source_date = ""
    header = None
    header_line = None
    for number, line in enumerate(rows, start=1):
        cols = _split_tsv(line)
        if len(cols) >= 2 and cols[0] == "数据日期":
            source_date = cols[1]
        if len(cols) >= 3 and cols[0] == "排名" and "代码" in cols and "名称" in cols:
            header = _header_map(cols)
            header_line = number
            break
    if not source_date:
        raise ValueError("未找到数据日期")
    if not header or header_line is None:
        raise ValueError("未找到包含代码/名称的表头")
    try:
        parsed_date = date.fromisoformat(source_date)
    except ValueError as exc:
        raise ValueError("数据日期格式无效，应为 YYYY-MM-DD") from exc
    code_index = header.get("代码")
    name_index = header.get("名称")
    board_index = header.get("板块")
    if code_index is None or name_index is None:
        raise ValueError("表头缺少代码或名称列")
    seen: set[str] = set()
    items = []
    errors = []
    for number, line in enumerate(rows[header_line:], start=header_line + 1):
        if not line.strip():
            continue
        cols = _split_tsv(line)
        code = cols[code_index] if code_index < len(cols) else ""
        name = cols[name_index] if name_index < len(cols) else ""
        board = cols[board_index] if board_index < len(cols) else ""
        if not code and not name:
            continue
        if len(items) + len(errors) >= MAX_CANDIDATES:
            errors.append({"line": number, "reason": f"候选超过 {MAX_CANDIDATES} 条上限"})
            break
        if len(code) != 6 or not code.isdigit():
            errors.append({"line": number, "code": code, "name": name, "reason": "代码列必须是 6 位数字字符串"})
            continue
        reason = reject_reason(code)
        exchange, board_name = classify(code)
        if reason:
            errors.append({"line": number, "code": code, "name": name, "reason": reason})
            continue
        if code in seen:
            errors.append({"line": number, "code": code, "name": name, "reason": "重复代码"})
            continue
        seen.add(code)
        items.append({
            "line": number,
            "code": code,
            "name": name,
            "market": exchange,
            "board": board_name,
            "board_label": BOARD_LABELS.get(board_name or "", board or board_name or ""),
            "selected": True,
        })
    return {
        "preview_version": PREVIEW_VERSION,
        "source_name": source_name,
        "source_date": parsed_date.isoformat(),
        "default_eval_date": parsed_date.isoformat(),
        "items": items,
        "errors": errors,
        "counts": {"valid": len(items), "invalid": len(errors), "total": len(items) + len(errors)},
    }


def preview_upload(payload: bytes, source_name: str = "") -> dict:
    return preview_text(_decode_text(payload), source_name)


def _item_payload(row: AiCouncilBatchItem) -> dict:
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "position": row.position,
        "stock_code": row.stock_code,
        "stock_name": row.stock_name,
        "source_market": row.source_market,
        "status": row.status,
        "stage": row.stage,
        "session_id": row.session_id,
        "probability_percent": row.probability_percent,
        "confidence": row.confidence or "",
        "summary": row.summary or "",
        "error": row.error or "",
        "details": row.details or {},
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _batch_payload(row: AiCouncilBatch, items: list[AiCouncilBatchItem]) -> dict:
    return {
        "id": row.id,
        "source_name": row.source_name,
        "source_date": row.source_date.isoformat() if row.source_date else None,
        "eval_date": row.eval_date.isoformat(),
        "status": row.status,
        "stage": row.stage,
        "profile_ids": list(row.profile_ids or []),
        "item_count": row.item_count,
        "done_count": row.done_count,
        "failed_count": row.failed_count,
        "cancelled_count": row.cancelled_count,
        "parser_summary": row.parser_summary or {},
        "error": row.error or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "items": [_item_payload(item) for item in items],
    }


def _idempotency_key(preview: dict, eval_date: str, profile_ids: list[str]) -> str:
    material = "|".join([preview.get("source_date") or "", eval_date, ",".join(profile_ids), ",".join(item["code"] for item in preview.get("items") or [])])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def create_batch(preview: dict, eval_date: str, profile_ids: list[str], source_name: str = "", idempotency_key: str | None = None) -> dict:
    if not isinstance(profile_ids, list) or any(not isinstance(item, str) for item in profile_ids):
        raise ValueError("profile_ids 要是字符串数组")
    profiles = service._valid_profiles(profile_ids)
    if len(profiles) < 2:
        raise ValueError("至少需要两个已填密钥的模型才能开会")
    try:
        target_day = date.fromisoformat(eval_date)
    except ValueError as exc:
        raise ValueError("eval_date 格式应为 YYYY-MM-DD") from exc
    if target_day > today():
        raise ValueError("不能用未来的日期")
    items = [item for item in (preview or {}).get("items") or [] if item.get("selected", True)]
    if not items:
        raise ValueError("没有可启动的有效股票")
    idempotency_key = idempotency_key or _idempotency_key(preview, eval_date, profile_ids)
    with session_scope() as session:
        existing = session.query(AiCouncilBatch).filter(AiCouncilBatch.idempotency_key == idempotency_key).first()
        if existing is not None:
            rows = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == existing.id).order_by(AiCouncilBatchItem.position.asc()).all()
            return _batch_payload(existing, rows)
        batch = AiCouncilBatch(
            source_name=source_name or preview.get("source_name") or "",
            source_date=date.fromisoformat(preview["source_date"]) if preview.get("source_date") else None,
            eval_date=target_day,
            status="pending",
            stage="待开始",
            profile_ids=list(profile_ids),
            profile_order_snapshot=service._public_participants(profiles),
            item_count=len(items),
            parser_summary={"preview_version": preview.get("preview_version"), "counts": preview.get("counts"), "errors": preview.get("errors")},
            idempotency_key=idempotency_key,
        )
        session.add(batch)
        session.commit()
        for index, item in enumerate(items, start=1):
            session.add(AiCouncilBatchItem(
                batch_id=batch.id,
                position=index,
                stock_code=item["code"],
                stock_name=item.get("name") or "",
                source_market=item.get("market") or "",
                status="pending",
                stage="待开始",
                details={"line": item.get("line"), "board": item.get("board"), "board_label": item.get("board_label")},
            ))
        session.commit()
        rows = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch.id).order_by(AiCouncilBatchItem.position.asc()).all()
        return _batch_payload(batch, rows)


def get_batch(batch_id: int) -> dict | None:
    with session_scope() as session:
        batch = session.query(AiCouncilBatch).filter(AiCouncilBatch.id == batch_id).first()
        if batch is None:
            return None
        items = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch_id).order_by(AiCouncilBatchItem.position.asc()).all()
        return _batch_payload(batch, items)


def list_batches(limit: int = 50) -> list[dict]:
    with session_scope() as session:
        batches = session.query(AiCouncilBatch).order_by(AiCouncilBatch.id.desc()).limit(limit).all()
        out = []
        for batch in batches:
            items = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch.id).order_by(AiCouncilBatchItem.position.asc()).all()
            out.append(_batch_payload(batch, items))
        return out


def cancel_batch(batch_id: int) -> dict:
    with session_scope() as session:
        batch = session.query(AiCouncilBatch).filter(AiCouncilBatch.id == batch_id).first()
        if batch is None:
            raise LookupError("没有这个批次")
        rows = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch_id).all()
        changed = 0
        for row in rows:
            if row.status == "pending":
                row.status = "cancelled"
                row.stage = "已取消"
                row.error = "用户取消未开始项"
                row.finished_at = _now()
                row.updated_at = _now()
                changed += 1
        batch.cancelled_count = len([row for row in rows if row.status == "cancelled"])
        batch.done_count = len([row for row in rows if row.status == "done"])
        batch.failed_count = len([row for row in rows if row.status == "failed"])
        batch.updated_at = _now()
        if all(row.status in {"done", "failed", "cancelled"} for row in rows):
            batch.status = "done"
            batch.stage = "已结束"
            batch.finished_at = _now()
        session.commit()
    result = get_batch(batch_id)
    assert result is not None
    result["cancelled_now"] = changed
    return result


def retry_failed(batch_id: int) -> dict:
    with session_scope() as session:
        batch = session.query(AiCouncilBatch).filter(AiCouncilBatch.id == batch_id).first()
        if batch is None:
            raise LookupError("没有这个批次")
        rows = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch_id).all()
        changed = 0
        for row in rows:
            if row.status == "failed":
                row.status = "pending"
                row.stage = "待重试"
                row.error = ""
                row.session_id = None
                row.probability_percent = None
                row.confidence = ""
                row.summary = ""
                row.started_at = None
                row.finished_at = None
                row.updated_at = _now()
                changed += 1
        if changed:
            batch.status = "pending"
            batch.stage = "待开始"
            batch.error = ""
            batch.finished_at = None
            batch.updated_at = _now()
        session.commit()
    result = get_batch(batch_id)
    assert result is not None
    result["retried"] = changed
    return result


def reconcile_stale_batches() -> int:
    fixed = 0
    with session_scope() as session:
        batches = session.query(AiCouncilBatch).filter(AiCouncilBatch.status == "running").all()
        for batch in batches:
            batch.status = "interrupted"
            batch.stage = "进程重启后中断"
            batch.error = "后台进程重启前遗留的批次已标记中断；不会自动重复调用付费模型"
            batch.finished_at = _now()
            batch.updated_at = _now()
            rows = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch.id, AiCouncilBatchItem.status.in_(("pending", "running"))).all()
            for row in rows:
                row.status = "interrupted" if row.status == "running" else "pending"
                row.stage = "等待用户决定是否继续"
                row.updated_at = _now()
            fixed += 1
        session.commit()
    return fixed


def _run_one_item(batch_id: int, item_id: int) -> None:
    with session_scope() as session:
        item = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.id == item_id).first()
        batch = session.query(AiCouncilBatch).filter(AiCouncilBatch.id == batch_id).first()
        if item is None or batch is None or item.status != "pending":
            return
        item.status = "running"
        item.stage = "准备启动"
        item.started_at = _now()
        item.updated_at = _now()
        batch.status = "running"
        batch.stage = f"排队执行 {item.stock_code}"
        batch.started_at = batch.started_at or _now()
        batch.updated_at = _now()
        session.commit()
    try:
        result = service.start_sync(item.stock_code, day=batch.eval_date, profile_ids=list(batch.profile_ids or []))
        session_id = int(result["session_id"])
        row = service.get(session_id)
        conclusion = row.get("conclusion") or {}
        with session_scope() as session:
            item = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.id == item_id).first()
            batch = session.query(AiCouncilBatch).filter(AiCouncilBatch.id == batch_id).first()
            assert item is not None and batch is not None
            item.session_id = session_id
            item.status = row.get("status") or "failed"
            item.stage = "完成" if item.status == "done" else "失败"
            item.probability_percent = conclusion.get("up_probability")
            item.confidence = conclusion.get("confidence") or ""
            item.summary = conclusion.get("summary") or ""
            item.error = row.get("error") or conclusion.get("probability_reason") or ""
            item.details = {"probability_status": conclusion.get("probability_status"), "session_deleted": False}
            item.finished_at = _now()
            item.updated_at = _now()
            rows = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch_id).all()
            batch.done_count = len([r for r in rows if r.status == "done"])
            batch.failed_count = len([r for r in rows if r.status == "failed"])
            batch.cancelled_count = len([r for r in rows if r.status == "cancelled"])
            batch.updated_at = _now()
            if all(r.status in {"done", "failed", "cancelled", "interrupted"} for r in rows):
                batch.status = "done"
                batch.stage = "已结束"
                batch.finished_at = _now()
            else:
                batch.status = "running"
                batch.stage = "继续下一股"
            session.commit()
    except Exception as exc:  # noqa: BLE001
        with session_scope() as session:
            item = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.id == item_id).first()
            batch = session.query(AiCouncilBatch).filter(AiCouncilBatch.id == batch_id).first()
            assert item is not None and batch is not None
            item.status = "failed"
            item.stage = "失败"
            item.error = str(exc)
            item.finished_at = _now()
            item.updated_at = _now()
            rows = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch_id).all()
            batch.done_count = len([r for r in rows if r.status == "done"])
            batch.failed_count = len([r for r in rows if r.status == "failed"])
            batch.cancelled_count = len([r for r in rows if r.status == "cancelled"])
            batch.status = "done" if all(r.status in {"done", "failed", "cancelled", "interrupted"} for r in rows) else "running"
            batch.stage = "已结束" if batch.status == "done" else "继续下一股"
            batch.error = "" if batch.status != "done" else batch.error
            batch.finished_at = _now() if batch.status == "done" else batch.finished_at
            batch.updated_at = _now()
            session.commit()


def _worker() -> None:
    while not _BATCH_STOP.is_set():
        pending: tuple[int, int] | None = None
        with session_scope() as session:
            batch = session.query(AiCouncilBatch).filter(AiCouncilBatch.status.in_(("pending", "running"))).order_by(AiCouncilBatch.id.asc()).first()
            if batch is not None:
                item = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch.id, AiCouncilBatchItem.status == "pending").order_by(AiCouncilBatchItem.position.asc()).first()
                if item is not None:
                    pending = (batch.id, item.id)
        if pending is None:
            _BATCH_STOP.wait(0.2)
            continue
        _run_one_item(*pending)


def ensure_worker() -> None:
    global _BATCH_THREAD
    with _BATCH_GUARD:
        if _BATCH_THREAD is not None and _BATCH_THREAD.is_alive():
            return
        _BATCH_STOP.clear()
        _BATCH_THREAD = threading.Thread(target=_worker, name="council-batch-worker", daemon=True)
        _BATCH_THREAD.start()


def stop_worker() -> None:
    _BATCH_STOP.set()


def verify_real_sample(path: str | Path) -> dict:
    payload = Path(path).read_bytes()
    preview = preview_upload(payload, Path(path).name)
    return {
        "source_date": preview["source_date"],
        "valid_count": preview["counts"]["valid"],
        "invalid_count": preview["counts"]["invalid"],
        "codes": [item["code"] for item in preview["items"]],
    }
