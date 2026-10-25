"""刚开机的机器上，两条后台线程叫得停、第一轮不白等。

`time.monotonic()` 在 Linux 上是开机秒数。云端 CI 的虚拟机开机才几十秒，
拿 `0.0` 当「没人在看 / 从没跑过」的哨兵在那里会同时坏两处：

* `stop()` 把读取时刻写成 0.0，`monotonic() - 0.0` 还没过 `IDLE_EXIT_SECONDS`（300 秒），
  线程不退，测试收尾等它死等到超时；
* `last_full_attempt_at or 0.0` 让「从没跑过」读成「0 秒时跑过一次」，
  开机两分钟内第一轮全市场不开，监控榜一直是 warming。

这里把两个模块看到的时钟换成「开机 30 秒、照常往前走」，各测一次「叫得停」与「第一轮立刻跑」，
再配一条正向对照：真没人读够 300 秒，照样退出——修哨兵不能把空闲退出修没了。
"""

import threading
import time

import pytest

from backend.app.core.constants import FULL_ROUND_SECONDS, IDLE_EXIT_SECONDS
from backend.app.services import intraday_watch, realtime_tracker
from backend.app.services.intraday_watch import IntradayWatcher
from backend.app.services.realtime_tracker import RealtimeTracker

UPTIME_SECONDS = 30.0

#: 判据前提：开机秒数要落在两道门槛之下，否则这份测试在旧代码上也是绿的，什么都没证明。
assert UPTIME_SECONDS < min(IDLE_EXIT_SECONDS, FULL_ROUND_SECONDS)


class _FreshBootTime:
    """只改 `monotonic`：从「开机 30 秒」起照常往前走；其余属性原样转给真 `time`。"""

    def __init__(self, uptime: float) -> None:
        self._offset = time.monotonic() - uptime

    def monotonic(self) -> float:
        return time.monotonic() - self._offset

    def __getattr__(self, name: str):
        return getattr(time, name)


def _wait(predicate, seconds: float) -> bool:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _dead(thread: threading.Thread | None) -> bool:
    return thread is None or not thread.is_alive()


@pytest.fixture()
def fresh_boot(monkeypatch):
    fake = _FreshBootTime(UPTIME_SECONDS)
    monkeypatch.setattr(intraday_watch, "time", fake)
    monkeypatch.setattr(realtime_tracker, "time", fake)
    assert fake.monotonic() < IDLE_EXIT_SECONDS  # 注入自证：两个模块此刻读到的确实是「刚开机」
    return fake


@pytest.fixture()
def watcher(fresh_boot, monkeypatch):
    monkeypatch.setattr(intraday_watch, "ALERT_POLL_SECONDS", 0.05)
    instance = IntradayWatcher()
    rounds = {"n": 0}

    def counted_round():
        rounds["n"] += 1
        return {}

    instance.run_round = counted_round  # 不碰库也不碰行情源：这里只测线程的生死
    yield instance, rounds
    instance.stop()
    _wait(lambda: _dead(instance._thread), 5.0)


@pytest.fixture()
def tracker(fresh_boot):
    instance = RealtimeTracker()
    rounds = {"n": 0}

    def counted_full_round():
        rounds["n"] += 1
        with instance._state_lock:
            instance.last_full_attempt_at = fresh_boot.monotonic()

    instance._run_full_round = counted_full_round  # 不扫全市场：这里只测「第一轮开不开」与生死
    yield instance, rounds
    instance.stop()
    _wait(lambda: _dead(instance._thread), 5.0)


def test_intraday_watcher_stops_on_fresh_boot(watcher):
    instance, rounds = watcher
    instance.keepalive()
    assert _wait(lambda: rounds["n"] >= 1, 3.0), "线程起来后一轮都没跑"

    instance.stop()
    assert _wait(lambda: _dead(instance._thread), 3.0), "刚开机时 stop() 叫不停报警榜线程"


def test_intraday_watcher_still_exits_when_idle(watcher):
    instance, rounds = watcher
    instance.keepalive()
    assert _wait(lambda: rounds["n"] >= 1, 3.0)

    instance._last_read_at = intraday_watch.time.monotonic() - IDLE_EXIT_SECONDS - 1
    assert _wait(lambda: _dead(instance._thread), 3.0), "没人读够 300 秒，报警榜线程却没退"


def test_realtime_tracker_first_round_runs_at_once_on_fresh_boot(tracker):
    instance, rounds = tracker
    assert instance.last_full_attempt_at is None
    instance._ensure_thread()
    assert _wait(lambda: rounds["n"] >= 1, 3.0), "刚开机时第一轮全市场没开，榜会一直是 warming"


def test_realtime_tracker_stops_on_fresh_boot(tracker):
    instance, rounds = tracker
    instance._ensure_thread()
    assert _wait(lambda: instance._thread is not None and instance._thread.is_alive(), 3.0)

    instance.stop()
    assert _wait(lambda: _dead(instance._thread), 3.0), "刚开机时 stop() 叫不停监控线程"


def test_realtime_tracker_still_exits_when_idle(tracker):
    instance, rounds = tracker
    instance._ensure_thread()
    assert _wait(lambda: rounds["n"] >= 1, 3.0)

    instance._last_read_at = realtime_tracker.time.monotonic() - IDLE_EXIT_SECONDS - 1
    assert _wait(lambda: _dead(instance._thread), 3.0), "没人读够 300 秒，监控线程却没退"
