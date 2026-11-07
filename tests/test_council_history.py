"""Isolated HTTP deletion tests; never operate on real user records or models."""
from datetime import date
import threading
import pytest
from backend.app.core.database import session_scope
from backend.app.models.tables import AiCouncilSession
from backend.app.services.council import service


def seed(status="done"):
    with session_scope() as session:
        row = AiCouncilSession(stock_code="600759", stock_name="测试会话", eval_date=date(2026, 11, 6),
                               status=status, participants=[], evidence={}, statements=[], transcript=[], error="")
        session.add(row)
        session.commit()
        return row.id


def test_delete_http_and_repeated_delete(client, monkeypatch):
    sid = seed()
    other = seed("failed")
    monkeypatch.setitem(service._PROGRESS, "session_id", sid)
    monkeypatch.setitem(service._PROGRESS, "running", False)
    response = client.delete(f"/api/council/{sid}")
    assert response.status_code == 200 and response.json() == {"deleted": True, "session_id": sid}
    assert client.get(f"/api/council/{sid}").status_code == 404
    assert client.delete(f"/api/council/{sid}").status_code == 404
    assert [row["id"] for row in client.get("/api/council/history").json()["items"]] == [other]
    assert service.progress()["session_id"] is None
    with pytest.raises(LookupError):
        service._persist_fields(sid, status="done")
    with pytest.raises(LookupError):
        service._append_transcript(sid, {"content": "late"})
    assert service.get(sid) is None


@pytest.mark.parametrize("status", ["running", "done"])
def test_delete_rejects_running_or_active_writer(client, status):
    sid = seed(status)
    if status == "done": service._mark_active(sid)
    try:
        assert client.delete(f"/api/council/{sid}").status_code == 409
        assert service.get(sid) is not None
    finally:
        service._unmark_active(sid)


def test_delete_rejects_held_session_lock(client):
    sid = seed()
    with service._session_lock(sid):
        assert client.delete(f"/api/council/{sid}").status_code == 409
    assert service.get(sid) is not None


def test_delete_serializes_with_manual_and_start_gate(client):
    sid = seed()
    with service._START_LOCK:
        assert client.delete(f"/api/council/{sid}").status_code == 409
    assert client.delete(f"/api/council/{sid}").status_code == 200


def test_delete_fail_preserves_record(client, monkeypatch):
    sid = seed()
    from sqlalchemy.orm import Query
    def fail(_self, **_kwargs): raise RuntimeError("simulated storage refusal")
    monkeypatch.setattr(Query, "delete", fail)
    assert client.delete(f"/api/council/{sid}").status_code == 409
    assert service.get(sid) is not None


def test_deleted_session_rejects_new_mutations(client):
    sid = seed()
    assert client.delete(f"/api/council/{sid}").status_code == 200
    assert client.post(f"/api/council/{sid}/user-message", json={"text": "hello"}).status_code == 404
    assert client.post(f"/api/council/{sid}/speak", json={"profile_id": "missing"}).status_code == 404


def test_created_identity_preserves_microseconds_for_reused_sqlite_id(client):
    from datetime import datetime
    sid = seed()
    with session_scope() as session:
        session.get(AiCouncilSession, sid).created_at = datetime(2026, 11, 7, 13, 0, 0, 1)
        session.commit()
    old = client.get(f"/api/council/{sid}").json()["created_at"]
    assert client.delete(f"/api/council/{sid}").status_code == 200
    new_id = seed()
    with session_scope() as session:
        session.get(AiCouncilSession, new_id).created_at = datetime(2026, 11, 7, 13, 0, 0, 2)
        session.commit()
    new = client.get(f"/api/council/{new_id}").json()["created_at"]
    assert old != new
    assert old.endswith("000001+08:00") and new.endswith("000002+08:00")


def test_delete_conditional_status_check_at_write_time(client, monkeypatch):
    from sqlalchemy.orm import Query
    sid = seed()
    original = Query.delete
    def raced(query, **kwargs):
        query.session.query(AiCouncilSession).filter_by(id=sid).update({"status": "running"})
        return original(query, **kwargs)
    monkeypatch.setattr(Query, "delete", raced)
    assert client.delete(f"/api/council/{sid}").status_code == 409
    assert service.get(sid) is not None
