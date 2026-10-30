"""同步和换源的健壮性：慢源、卡死源、5xx 不能拖住接口；能取消；换源立刻生效并持久化。"""

import threading
import time
from datetime import date

from backend.app.providers import router as provider_router
from backend.app.services import sync_service as sync
from tests.test_source_fallback import _bar


def _wait(client, predicate, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        state = client.get("/api/sync/status").json()
        if predicate(state):
            return state
        time.sleep(0.05)
    raise AssertionError(f"等待超时：{state}")


def test_timeouts_trip_the_breaker_and_skip_the_source(monkeypatch):
    sync._clear_source_health()
    calls = []

    def kline_of(name):
        def fetch(code, start, end):
            calls.append(name)
            if name == "eastmoney":
                raise TimeoutError("read timed out")
            return [_bar(amount=1, turnover_rate=1)]
        return fetch

    monkeypatch.setattr(sync, "_kline_of", kline_of)
    monkeypatch.setattr(sync, "_pace", lambda: None)
    for index in range(10):
        source, _ = sync.fetch_from_chain(f"{index:06d}", date(2026, 10, 1), date(2026, 10, 22), ["eastmoney", "sina"])
        assert source == "sina"
    assert calls.count("eastmoney") == sync.BREAKER_THRESHOLD
    assert sync._is_sick("eastmoney")
    sync._clear_source_health()


def test_slow_sync_keeps_api_responsive_and_can_be_cancelled(client):
    stocks = [(f"{600000 + i}", f"股票{i}") for i in range(400)]
    gate = threading.Event()

    def slow_kline(code, start, end):
        gate.wait(0.2)
        return []

    sync.set_fetcher(lambda: stocks, slow_kline)
    first = client.post("/api/sync").json()
    second = client.post("/api/sync").json()
    assert second["task_id"] == first["task_id"] and second["already_running"] is True
    _wait(client, lambda s: s["done"] > 0)
    began = time.monotonic()
    for path in ("/api/health", "/api/settings", "/api/sync/status"):
        assert client.get(path).status_code == 200
    assert time.monotonic() - began < 2.0
    assert client.post("/api/sync/cancel").status_code == 200
    state = _wait(client, lambda s: not s["running"])
    assert state["phase"] == "cancelled"
    assert state["done"] < len(stocks)
    sync.set_fetcher()


def test_switch_source_persists_and_applies_mid_run(client, monkeypatch):
    saved = client.put("/api/settings", json={"data_source": "sina"}).json()
    assert saved["data_source"] == "sina"
    assert client.get("/api/settings").json()["data_source"] == "sina"
    sync._clear_source_health()
    monkeypatch.setattr(sync, "_OVERRIDE", False)
    assert sync._source_chain() == ["sina"]
    client.put("/api/settings", json={"data_source": "tencent"})
    assert sync._source_chain() == ["tencent"]
    client.put("/api/settings", json={"data_source": "auto"})
    assert sync._source_chain()[0] in {"eastmoney", "tencent", "sina"}
    assert client.put("/api/settings", json={"data_source": "nope"}).status_code == 422
    sync._clear_source_health()


def test_source_test_has_a_deadline(client, monkeypatch):
    class Hanging:
        @staticmethod
        def fetch_kline(code, start, end):
            time.sleep(30)

    class Broken:
        @staticmethod
        def fetch_kline(code, start, end):
            raise RuntimeError("HTTP 502")

    class Good:
        @staticmethod
        def fetch_kline(code, start, end):
            return [{"trade_date": date(2026, 10, 29)}]

    modules = {"eastmoney": Broken, "tencent": Hanging, "sina": Good}
    monkeypatch.setattr(provider_router, "_module", lambda name: modules[name])
    began = time.monotonic()
    payload = client.post("/api/sources/test", json={"timeout": 2}).json()
    assert time.monotonic() - began < 4
    results = {row["source"]: row for row in payload["results"]}
    assert results["sina"]["ok"] is True
    assert results["eastmoney"]["ok"] is False and "502" in results["eastmoney"]["error"]
    assert results["tencent"]["ok"] is False


def test_probe_deadline(monkeypatch):
    monkeypatch.setattr(sync, "PROBE_DEADLINE", 0.5)
    try:
        sync._probe_with_deadline(lambda name: time.sleep(5), "eastmoney")
    except TimeoutError as exc:
        assert "eastmoney" in str(exc)
    else:
        raise AssertionError("应当超时")
