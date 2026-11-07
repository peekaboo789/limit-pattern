"""v0.4.3 回测缓存预热：后台算、请求等预热不重复算、设置改了重新预热。"""

import threading
import time

from backend.app.core.database import session_scope
from backend.app.services import backtest_service as bt
from tests.test_ranking_monitor import _seed_many


def _computes():
    return bt._CACHE.get("computes", 0)


def test_warm_then_request_uses_cache(client):
    _seed_many()
    bt._CACHE.clear()
    thread = bt.warm_cache()
    assert thread is not None
    thread.join(60)
    assert bt.warm_status()["running"] is False and bt.warm_status()["last_error"] == ""
    n = _computes()
    with session_scope() as s:
        bt.band_lifts(s)
    assert _computes() == n == 1


def test_request_waits_for_running_warm(client, monkeypatch):
    _seed_many()
    bt._CACHE.clear()
    real = bt._similarity_backtest
    calls = []

    def slow(*a, **k):
        calls.append(1)
        time.sleep(1.0)
        return real(*a, **k)

    monkeypatch.setattr(bt, "_similarity_backtest", slow)
    thread = bt.warm_cache()
    time.sleep(0.2)
    assert bt.warm_cache() is None  # 已在预热，不再开第二个
    out = {}

    def user():
        with session_scope() as s:
            out["v"] = bt.band_lifts(s)

    t = threading.Thread(target=user)
    t.start()
    t.join(60)
    thread.join(60)
    assert len(calls) == 1 and "v" in out


def test_settings_change_invalidates_and_rewarms(client, monkeypatch):
    _seed_many()
    bt._CACHE.clear()
    bt.warm_cache(sync=True)
    first = bt._CACHE["key"]
    monkeypatch.setenv("LP_WARM", "1")
    started = []
    monkeypatch.setattr(bt, "warm_cache", lambda sync=False: started.append(1))
    assert client.put("/api/settings", json={"similarity_threshold_high": 85}).status_code == 200
    assert started == [1]
    client.put("/api/settings", json={"ai_model": "x"})
    assert started == [1]  # 无关设置不触发
    with session_scope() as s:
        bt.similarity_backtest(s, top=100)
    assert bt._CACHE["key"] != first and _computes() == 2
    assert client.get("/api/backtest/warm").status_code == 200


def test_sync_finish_triggers_warm(monkeypatch):
    from backend.app.services import sync_service

    monkeypatch.setenv("LP_WARM", "1")
    hits = []
    monkeypatch.setattr(bt, "warm_cache", lambda sync=False: hits.append(1))
    sync_service._warm_after_change()
    assert hits == [1]
