from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import threading
import time

import anyio
import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.core.database import session_scope
from backend.app.models.tables import AiCouncilBatch, AiCouncilBatchItem, DailyQuote, Stock, TradingDay
from backend.app.services.council import batch_service, service


def _reset_council_progress():
    with service._LOCK:
        service._PROGRESS.update(running=False, phase="idle", session_id=None, stock=None, stage="空闲", current_model="", done=0, total=0, last_status="", error="", started_at=None, finished_at=None, elapsed_seconds=None)


@pytest.fixture(autouse=True)
def _reset_council_runtime_state():
    _reset_council_progress()
    yield
    _reset_council_progress()


def _seed_quotes(day: date = date(2026, 11, 6)):
    _reset_council_progress()
    days = []
    cursor = day
    while len(days) < 8:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days = sorted(days)
    with session_scope() as session:
        for code, name in [("000001", "平安银行"), ("000828", "东莞控股"), ("000755", "山西高速"), ("600320", "振华重工")]:
            if session.get(Stock, code) is None:
                session.add(Stock(code=code, name=name, exchange="SZSE" if code.startswith("0") else "SSE", board="main_board"))
        for d in days:
            if session.get(TradingDay, d) is None:
                session.add(TradingDay(trade_date=d, is_open=True, limit_up_count=0))
            for code in ["000001", "000828", "000755", "600320"]:
                exists = session.query(DailyQuote).filter(DailyQuote.stock_code == code, DailyQuote.trade_date == d).first()
                if exists:
                    continue
                session.add(DailyQuote(stock_code=code, trade_date=d, open=10, high=11, low=9, close=10.5, pre_close=10,
                                       volume=1000, amount=10000, turnover_rate=1.2, change_percent=0.5,
                                       is_limit_up=False, is_limit_down=False, limit_up_price=11, limit_down_price=9,
                                       source="test"))
        session.commit()


def _profiles(client):
    for index, model in enumerate(["m-a", "m-b"]):
        response = client.post("/api/ai/profiles", json={
            "name": f"模型{index+1}", "base_url": "https://example.invalid/v1", "model": model, "api_key": f"sk-fake-{index}"
        })
        assert response.status_code == 200, response.text
    data = client.get("/api/ai/profiles").json()
    return [item["id"] for item in data["items"][-2:]]


def test_preview_real_format_and_no_sample_column_leak():
    preview = batch_service.preview_upload(Path("/workspace/limit-pattern-council-batch-sample.txt").read_bytes(), "sample.txt")
    assert preview["source_date"] == "2026-11-06"
    assert preview["counts"]["valid"] == 20
    codes = [item["code"] for item in preview["items"]]
    assert codes[0] == "000001"
    assert "000828" in codes and "000755" in codes
    assert "000498" not in codes
    assert "600241" not in codes


def test_preview_rejects_non_utf8_and_long_line():
    with pytest.raises(ValueError, match="UTF-8"):
        batch_service.preview_upload("\xff\xfe\x00".encode("latin1"), "bad.txt")
    text = "数据日期\t2026-11-06\n" + ("a" * 4097)
    with pytest.raises(ValueError, match="4096"):
        batch_service.preview_upload(text.encode("utf-8"), "long.txt")


def test_preview_preserves_leading_zero_and_rejects_duplicates():
    text = "生成时间\t2026-11-07\n数据日期\t2026-11-06\n\n排名\t代码\t名称\t板块\n1\t000001\t平安银行\t主板\n2\t000001\t平安银行\t主板\n"
    preview = batch_service.preview_upload(text.encode("utf-8"), "dup.txt")
    assert preview["items"][0]["code"] == "000001"
    assert preview["errors"][0]["reason"] == "重复代码"


def test_batch_create_idempotent(client):
    _seed_quotes()
    profile_ids = _profiles(client)
    preview = batch_service.preview_upload(Path("/workspace/limit-pattern-council-batch-sample.txt").read_bytes(), "sample.txt")
    first = batch_service.create_batch(preview, "2026-11-06", profile_ids, "sample.txt", "idem-1")
    second = batch_service.create_batch(preview, "2026-11-06", profile_ids, "sample.txt", "idem-1")
    assert first["id"] == second["id"]
    with session_scope() as session:
        assert session.query(AiCouncilBatch).count() == 1


def test_cancel_only_pending_items(client):
    _seed_quotes()
    profile_ids = _profiles(client)
    preview = {"source_date": "2026-11-06", "items": [{"code": "000001", "name": "平安银行"}, {"code": "000828", "name": "东莞控股"}]}
    batch = batch_service.create_batch(preview, "2026-11-06", profile_ids, "x.txt", "idem-cancel")
    with session_scope() as session:
        first = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch["id"], AiCouncilBatchItem.position == 1).first()
        assert first is not None
        first.status = "running"
        session.commit()
    result = batch_service.cancel_batch(batch["id"])
    statuses = {item["position"]: item["status"] for item in result["items"]}
    assert statuses[1] == "running"
    assert statuses[2] == "cancelled"


def test_retry_failed_only(client):
    _seed_quotes()
    profile_ids = _profiles(client)
    preview = {"source_date": "2026-11-06", "items": [{"code": "000001", "name": "平安银行"}, {"code": "000828", "name": "东莞控股"}]}
    batch = batch_service.create_batch(preview, "2026-11-06", profile_ids, "x.txt", "idem-retry")
    with session_scope() as session:
        rows = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch["id"]).order_by(AiCouncilBatchItem.position.asc()).all()
        rows[0].status = "done"
        rows[1].status = "failed"
        rows[1].session_id = 123
        session.commit()
    result = batch_service.retry_failed(batch["id"])
    assert result["retried"] == 1
    statuses = {item["position"]: item["status"] for item in result["items"]}
    assert statuses[1] == "done"
    assert statuses[2] == "pending"


def test_reconcile_stale_batches_marks_interrupted(client):
    _seed_quotes()
    profile_ids = _profiles(client)
    batch = batch_service.create_batch({"source_date": "2026-11-06", "items": [{"code": "000001", "name": "平安银行"}]}, "2026-11-06", profile_ids, "x.txt", "idem-stale")
    with session_scope() as session:
        row = session.query(AiCouncilBatch).filter(AiCouncilBatch.id == batch["id"]).first()
        item = session.query(AiCouncilBatchItem).filter(AiCouncilBatchItem.batch_id == batch["id"]).first()
        row.status = "running"
        assert item is not None
        item.status = "running"
        session.commit()
    assert batch_service.reconcile_stale_batches() == 1
    payload = batch_service.get_batch(batch["id"])
    assert payload is not None
    assert payload["status"] == "interrupted"
    assert payload["items"][0]["status"] == "interrupted"


def test_batch_worker_uses_single_session_run_and_never_imports_prediction_score(client, monkeypatch):
    _seed_quotes()
    profile_ids = _profiles(client)
    batch = batch_service.create_batch({
        "source_date": "2026-11-06",
        "items": [{"code": "000001", "name": "平安银行", "prediction_score": 99.9}]
    }, "2026-11-06", profile_ids, "x.txt", "idem-run")

    def fake_run(session_id, code, name, day, profiles, evidence):
        with session_scope() as session:
            row = session.get(service.AiCouncilSession, session_id)
            assert row is not None
            row.status = "done"
            row.conclusion = {"up_probability": None, "confidence": "low", "summary": "证据不足", "probability_status": "insufficient_evidence"}
            row.error = ""
            session.commit()

    monkeypatch.setattr(service, "_run", fake_run)
    batch_service._run_one_item(batch["id"], batch["items"][0]["id"])
    payload = batch_service.get_batch(batch["id"])
    assert payload is not None
    item = payload["items"][0]
    assert item["probability_percent"] is None
    assert item["details"]["probability_status"] == "insufficient_evidence"


def test_single_session_lock_wait_timeout_surfaces_as_failed_batch_item(client, monkeypatch):
    _seed_quotes()
    profile_ids = _profiles(client)
    batch = batch_service.create_batch({"source_date": "2026-11-06", "items": [{"code": "000001", "name": "平安银行"}]}, "2026-11-06", profile_ids, "x.txt", "idem-lock")
    monkeypatch.setattr(batch_service, "LOCK_WAIT_SECONDS", 0.01)
    hold = service._START_LOCK.acquire(timeout=0.01)
    assert hold
    try:
        batch_service._run_one_item(batch["id"], batch["items"][0]["id"])
    finally:
        service._START_LOCK.release()
    payload = batch_service.get_batch(batch["id"])
    assert payload is not None
    assert payload["items"][0]["status"] == "failed"
    assert "等待超过" in payload["items"][0]["error"]


def test_batch_preview_http_413_by_content_length(client, monkeypatch):
    monkeypatch.setattr(batch_service, "MAX_UPLOAD_BYTES", 16)
    response = client.post("/api/council/batch/preview", content=b"1234567890", headers={"content-length": "17"})
    assert response.status_code == 413
    assert "16" in response.json()["detail"]


def test_batch_preview_http_422_for_non_utf8(client):
    response = client.post("/api/council/batch/preview", content=b"\xff\xfe\x00")
    assert response.status_code == 422
    assert "UTF-8" in response.json()["detail"]


@pytest.mark.anyio
async def test_batch_preview_http_413_for_stream_without_content_length(tmp_path, monkeypatch):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LP_SCHEDULER", "0")
    monkeypatch.setenv("LP_WARM", "0")
    monkeypatch.setenv("LP_AUTO_SYNC", "0")
    monkeypatch.setenv("LP_STATIC_DIR", str(tmp_path / "missing-static"))
    from backend.app.core.database import init_db, reset_engine
    from backend.app.main import create_app
    from backend.app.services.sync_service import reset_state, set_fetcher

    reset_engine()
    set_fetcher()
    reset_state()
    init_db()
    monkeypatch.setattr(batch_service, "MAX_UPLOAD_BYTES", 16)

    async def body_stream():
        yield b"1234567890"
        yield b"ABCDEFGH"

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/council/batch/preview", content=body_stream(), headers={"x-file-name": "big.txt"})
    reset_engine()
    set_fetcher()
    assert response.status_code == 413
    assert "16" in response.json()["detail"]


def test_batch_start_http_422_rejects_bad_schema(client):
    response = client.post("/api/council/batch/start", json={"preview": {"source_date": "2026-11-06", "items": []}, "eval_date": 20261106, "profile_ids": "bad"})
    assert response.status_code == 422


def test_batch_start_rejects_same_key_different_payload(client):
    _seed_quotes()
    profile_ids = _profiles(client)
    first = {"source_date": "2026-11-06", "items": [{"code": "000001", "name": "平安银行", "selected": True}]}
    second = {"source_date": "2026-11-06", "items": [{"code": "000828", "name": "东莞控股", "selected": True}]}
    batch_service.create_batch(first, "2026-11-06", profile_ids, "x.txt", "idem-collision")
    with pytest.raises(ValueError, match="幂等键"):
        batch_service.create_batch(second, "2026-11-06", profile_ids, "x.txt", "idem-collision")


def test_batch_create_concurrent_same_key_returns_one_batch(client):
    _seed_quotes()
    profile_ids = _profiles(client)
    preview = {"source_date": "2026-11-06", "items": [{"code": "000001", "name": "平安银行", "selected": True}]}
    results = []
    errors = []

    def worker():
        try:
            results.append(batch_service.create_batch(preview, "2026-11-06", profile_ids, "x.txt", "idem-race"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(results) == 2
    assert results[0]["id"] == results[1]["id"]
    with session_scope() as session:
        assert session.query(AiCouncilBatch).count() == 1
        assert session.query(AiCouncilBatchItem).count() == 1


def test_batch_running_item_persists_session_id_early(client, monkeypatch):
    _seed_quotes()
    profile_ids = _profiles(client)
    batch = batch_service.create_batch({"source_date": "2026-11-06", "items": [{"code": "000001", "name": "平安银行", "selected": True}]}, "2026-11-06", profile_ids, "x.txt", "idem-live")
    seen = {}

    def fake_run(session_id, code, name, day, profiles, evidence):
        seen["session_id"] = session_id
        with session_scope() as session:
            row = session.get(service.AiCouncilSession, session_id)
            assert row is not None
            row.progress = {"stage": "第 1 位模型发言中", "current_model": "模型1"}
            session.commit()
        payload = batch_service.get_batch(batch["id"])
        assert payload is not None
        seen["item_session_id"] = payload["items"][0]["session_id"]
        seen["item_status"] = payload["items"][0]["status"]
        seen["item_stage"] = payload["items"][0]["stage"]
        seen["item_current_model"] = payload["items"][0]["details"].get("current_model")
        with session_scope() as session:
            row = session.get(service.AiCouncilSession, session_id)
            assert row is not None
            row.status = "done"
            row.conclusion = {"up_probability": 50, "confidence": "medium", "summary": "测试", "probability_status": "estimated"}
            session.commit()
        with service._LOCK:
            service._PROGRESS.update(running=False, phase="done", stage="完成", current_model="", done=len(profiles), total=len(profiles), last_status="", error="", finished_at=None, elapsed_seconds=None)

    monkeypatch.setattr(service, "_run", fake_run)
    batch_service._run_one_item(batch["id"], batch["items"][0]["id"])
    assert seen["item_session_id"] == seen["session_id"]
    assert seen["item_status"] == "running"
    assert seen["item_stage"] == "第 1 位模型发言中"
    assert seen["item_current_model"] == "模型1"


def test_batch_waits_for_single_session_lock_instead_of_immediate_fail(client, monkeypatch):
    _seed_quotes()
    profile_ids = _profiles(client)
    batch = batch_service.create_batch({"source_date": "2026-11-06", "items": [{"code": "000001", "name": "平安银行", "selected": True}]}, "2026-11-06", profile_ids, "x.txt", "idem-wait")
    monkeypatch.setattr(batch_service, "LOCK_WAIT_SECONDS", 0.3)
    hold = service._START_LOCK.acquire(timeout=0.01)
    assert hold

    def release_later():
        time.sleep(0.05)
        service._START_LOCK.release()

    def fake_run(session_id, code, name, day, profiles, evidence):
        with session_scope() as session:
            row = session.get(service.AiCouncilSession, session_id)
            assert row is not None
            row.status = "done"
            row.conclusion = {"up_probability": 50, "confidence": "medium", "summary": "测试", "probability_status": "estimated"}
            session.commit()

    releaser = threading.Thread(target=release_later)
    releaser.start()
    monkeypatch.setattr(service, "_run", fake_run)
    batch_service._run_one_item(batch["id"], batch["items"][0]["id"])
    releaser.join()
    payload = batch_service.get_batch(batch["id"])
    assert payload is not None
    assert payload["items"][0]["status"] == "done", payload["items"][0]["error"]


def test_verify_real_sample_reports_20_codes():
    result = batch_service.verify_real_sample("/workspace/limit-pattern-council-batch-sample.txt")
    assert result["source_date"] == "2026-11-06"
    assert result["valid_count"] == 20
    assert "000001" in result["codes"]
    assert "000498" not in result["codes"]
