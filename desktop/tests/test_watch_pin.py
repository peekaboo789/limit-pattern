"""桌面层只做一件事：把后端那份报警账落到窗口上。判据一个都不在这里重算。

每条用例都对着 `desktop/watch_pin.py` 文档里的一句承诺，负向控制也照着写：
「读不到维持上一份」必须再配一次「读到 `pinning=False` 才撤置顶」的对照，
否则窗口根本没摞上去时这条断言也是绿的。
"""

from __future__ import annotations

import time
from pathlib import Path

from desktop.watch_pin import (
    DEFAULT_POLL_SECONDS,
    ENDPOINT,
    MAX_POLL_SECONDS,
    MIN_POLL_SECONDS,
    WatchPin,
    alert_key,
    alert_text,
    title_of,
)

ROOT = Path(__file__).resolve().parents[2]
TRADE_DATE = "2026-10-23"


class FakeWindow:
    """假 pywebview 窗口：记下每一次置顶与改名，而不是只记最终值。"""

    def __init__(self) -> None:
        self.pin_log: list[bool] = []
        self.title_log: list[str] = []
        self._on_top = False
        self._title = ""
        self.destroyed = False
        self.reject_pin = False

    @property
    def on_top(self) -> bool:
        return self._on_top

    @on_top.setter
    def on_top(self, value: bool) -> None:
        if self.reject_pin:
            raise RuntimeError("WinForms 拒了这次置顶")
        self._on_top = bool(value)
        self.pin_log.append(bool(value))

    def set_title(self, title: str) -> None:
        self._title = title
        self.title_log.append(title)

    def destroy(self) -> None:
        self.destroyed = True


class FakeIcon:
    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []
        self.stopped = False
        self.reject_notify = False

    def notify(self, message: str, title: str | None = None) -> None:
        if self.reject_notify:
            raise RuntimeError("托盘还没就绪")
        self.notes.append((message, title or ""))

    def stop(self) -> None:
        self.stopped = True


class FakeState:
    """`WatchPin` 只认这三样：`window` / `icon` / `quitting`。"""

    def __init__(self, window=None, icon=None) -> None:
        self.window = window
        self.icon = icon
        self.quitting = False
        self.url = "http://127.0.0.1:9/"


def one_alert(
    *,
    code: str = "600111",
    name: str = "测试股",
    tier: str = "spike_6pct",
    label: str = "涨过 6% 且近 3 分钟跳升",
    percent: float | None = 7.20,
    trade_date: str = TRADE_DATE,
) -> dict:
    item = {
        "id": 1,
        "trade_date": trade_date,
        "stock_code": code,
        "stock_name": name,
        "tier": tier,
        "tier_label": label,
        "percent": percent,
        "rank": 4,
        "rise_3min": 2.4,
        "fired_at": "2026-10-23T10:15:00+08:00",
        "acked_at": None,
    }
    return item


def state_payload(
    *,
    pinning: bool = False,
    unacked: list[dict] | None = None,
    trade_date: str = TRADE_DATE,
    poll_seconds: object = DEFAULT_POLL_SECONDS,
) -> dict:
    """照着后端回包写：`pinning` 与 `unacked` 各自给，别让测试替桌面层推一个数。"""
    items = list(unacked or [])
    return {
        "status": "watching",
        "trade_date": trade_date,
        "note": "盘中监控中",
        "pinning": pinning,
        "pending_pin_codes": sorted({item["stock_code"] for item in items}),
        "unacked": items,
        "alerts": items,
        "poll_seconds": poll_seconds,
    }


def make_pin(state: FakeState, payloads: list[dict]) -> WatchPin:
    """按 `payloads` 排队读数；队列读完还继续读时抛错，模拟「读不到」。"""
    queue = list(payloads)

    def fetch() -> dict:
        if not queue:
            raise RuntimeError("上游没回话")
        return queue.pop(0)

    return WatchPin(state, base_url=state.url, base_title="三日涨停形态统计", fetch=fetch)


# --- 纯函数：文案与身份键都不许掺进判据 --------------------------------------


def test_alert_text_uses_only_backend_fields():
    text = alert_text(one_alert(label="涨过 6% 且近 3 分钟跳升", percent=7.2))
    assert text == "测试股（600111）涨过 6% 且近 3 分钟跳升：今日 7.20%"
    # 名字没给时退回代码，没有数字时不补一个 0.00%
    assert alert_text({"stock_code": "000001", "tier_label": "涨过 3%"}) == "000001（000001）涨过 3%"


def test_alert_key_is_per_day_and_per_tier():
    key = alert_key(one_alert())
    assert key == f"{TRADE_DATE}|600111|spike_6pct"
    assert alert_key(one_alert(trade_date="2026-10-22")) != key
    assert alert_key(one_alert(tier="popup_3pct")) != key


def test_title_shows_count_and_restores():
    base = "三日涨停形态统计"
    assert title_of(base, 0) == base
    assert title_of(base, 2) == "⚠ 涨幅报警 2 条待确认 · " + base


# --- 置顶与标题 --------------------------------------------------------------


def test_first_read_pins_but_does_not_notify():
    """开机第一遍读到的旧账只摞窗口、不弹托盘：否则每次启动都把没确认的挨个弹一遍。"""
    window = FakeWindow()
    icon = FakeIcon()
    state = FakeState(window=window, icon=icon)
    pin = make_pin(state, [state_payload(pinning=True, unacked=[one_alert(), one_alert(code="600112")])])
    pin.apply(pin._fetch())  # noqa: SLF001
    assert window.pin_log == [True]
    assert window.title_log == ["⚠ 涨幅报警 2 条待确认 · 三日涨停形态统计"]
    assert icon.notes == []


def test_new_alert_notifies_once_and_ack_releases_pin():
    window = FakeWindow()
    icon = FakeIcon()
    state = FakeState(window=window, icon=icon)
    first = one_alert(code="600111")
    second = one_alert(code="600112")
    payloads = [
        state_payload(pinning=True, unacked=[first]),  # 第一遍：旧账，静默
        state_payload(pinning=True, unacked=[first, second]),  # 新来一条：弹一次
        state_payload(pinning=True, unacked=[first, second]),  # 同一份账再读：不该再弹
        state_payload(pinning=False, unacked=[]),  # 用户点了「知道了」
    ]
    pin = make_pin(state, payloads)
    for _ in range(4):
        pin.apply(pin._fetch())  # noqa: SLF001
    assert icon.notes == [(alert_text(second), "三日涨停形态统计")]
    assert window.pin_log == [True, False]
    assert window.title_log == [
        "⚠ 涨幅报警 1 条待确认 · 三日涨停形态统计",
        "⚠ 涨幅报警 2 条待确认 · 三日涨停形态统计",
        "三日涨停形态统计",
    ]


def test_read_failure_keeps_the_pin_and_a_good_read_clears_it():
    """负向控制：读失败那一拍什么都不撤；读到 `pinning=False` 才撤。"""
    window = FakeWindow()
    state = FakeState(window=window, icon=FakeIcon())
    pin = WatchPin(
        state,
        base_url=state.url,
        base_title="三日涨停形态统计",
        fetch=lambda: state_payload(pinning=True, unacked=[one_alert()]),
    )
    pin.apply(pin._fetch())  # noqa: SLF001
    pin._first_read = False  # noqa: SLF001
    assert window.pin_log == [True]

    def boom() -> dict:
        pin.stop_event.set()  # 跑一轮就退，别在测试里真睡三秒
        raise OSError("服务没应答")

    pin._fetch = boom  # noqa: SLF001
    pin.run()
    assert window.pin_log == [True]
    assert pin.reads == 0

    def cleared() -> dict:
        pin.stop_event.set()
        return state_payload(pinning=False, unacked=[])

    pin._fetch = cleared  # noqa: SLF001
    pin.stop_event.clear()
    pin.run()
    assert window.pin_log == [True, False]
    assert window.title_log[-1] == "三日涨停形态统计"


def test_run_exits_immediately_when_the_launcher_is_quitting():
    state = FakeState(window=FakeWindow(), icon=FakeIcon())
    state.quitting = True
    pin = WatchPin(state, base_url=state.url, base_title="标题", fetch=lambda: {})
    pin.run()
    assert pin.reads == 0


def test_tier_ids_are_not_hardcoded_in_the_desktop_layer():
    """桌面层不认识档位名：只按后端的 `pinning` 摞窗口，文案照抄 `tier_label`。

    负向半边才是重点：一条没确认的报警配上 `pinning=False`（后端在「这一档不置顶」时就这么回）
    时窗口不许摞上去——否则这里就藏了一条自创判据「有报警就置顶」。
    """
    window = FakeWindow()
    icon = FakeIcon()
    state = FakeState(window=window, icon=icon)
    weird = one_alert(tier="明天可能新增的档位", label="后端自己起的名字", percent=None)
    pin = WatchPin(
        state,
        base_url=state.url,
        base_title="三日涨停形态统计",
        fetch=lambda: state_payload(pinning=False, unacked=[weird]),
    )
    pin._first_read = False  # noqa: SLF001
    pin.apply(pin._fetch())  # noqa: SLF001
    assert window.pin_log == []
    assert window.title_log == ["⚠ 涨幅报警 1 条待确认 · 三日涨停形态统计"]
    pin._fetch = lambda: state_payload(pinning=True, unacked=[weird])  # noqa: SLF001
    pin.apply(pin._fetch())  # noqa: SLF001
    assert window.pin_log == [True]
    assert icon.notes == [("测试股（600111）后端自己起的名字", "三日涨停形态统计")]


def test_day_rollover_notifies_the_same_code_again():
    """跨零点还活着的进程必须把今天当新的一天：去重键带交易日，旧账会被清掉。"""
    icon = FakeIcon()
    state = FakeState(window=FakeWindow(), icon=icon)
    yesterday = one_alert(trade_date="2026-10-22")
    today = one_alert(trade_date=TRADE_DATE)
    pin = WatchPin(
        state,
        base_url=state.url,
        base_title="三日涨停形态统计",
        fetch=lambda: state_payload(pinning=True, unacked=[yesterday]),
    )
    pin.apply(state_payload(pinning=True, unacked=[yesterday]))
    assert icon.notes == []
    pin._fetch = lambda: state_payload(pinning=True, unacked=[today], trade_date=TRADE_DATE)  # noqa: SLF001
    pin.apply(pin._fetch())  # noqa: SLF001
    assert len(icon.notes) == 1
    pin.apply(pin._fetch())  # noqa: SLF001
    assert len(icon.notes) == 1


# --- 窗口缺席、置顶被拒、托盘没就绪 ------------------------------------------


def test_missing_window_is_a_no_op_until_it_appears():
    window = FakeWindow()
    state = FakeState(window=None, icon=FakeIcon())
    pin = WatchPin(
        state, base_url=state.url, base_title="三日涨停形态统计",
        fetch=lambda: state_payload(pinning=True, unacked=[one_alert()]),
    )
    pin.apply(pin._fetch())  # noqa: SLF001
    assert window.pin_log == []
    state.window = window
    pin.apply(pin._fetch())  # noqa: SLF001
    assert window.pin_log == [True]


def test_rejected_pin_is_retried_next_round():
    window = FakeWindow()
    window.reject_pin = True
    state = FakeState(window=window, icon=FakeIcon())
    pin = WatchPin(
        state, base_url=state.url, base_title="三日涨停形态统计",
        fetch=lambda: state_payload(pinning=True, unacked=[one_alert()]),
    )
    pin.apply(pin._fetch())  # noqa: SLF001
    assert window.pin_log == []
    assert pin._pinned is False  # noqa: SLF001 - 没摞上就不许记账说摞上了
    window.reject_pin = False
    pin.apply(pin._fetch())  # noqa: SLF001
    assert window.pin_log == [True]


def test_notify_without_tray_falls_back_to_log(caplog):
    state = FakeState(window=FakeWindow(), icon=None)
    payloads = [state_payload(pinning=False, unacked=[]), state_payload(pinning=True, unacked=[one_alert()])]
    pin = make_pin(state, payloads)
    pin._first_read = False  # noqa: SLF001
    for _ in range(2):
        pin.apply(pin._fetch())  # noqa: SLF001
    with caplog.at_level("INFO"):
        pin._notify("测试一句话")  # noqa: SLF001
    assert "无托盘" in caplog.text


def test_failing_tray_does_not_break_the_loop(caplog):
    icon = FakeIcon()
    icon.reject_notify = True
    state = FakeState(window=FakeWindow(), icon=icon)
    pin = WatchPin(
        state, base_url=state.url, base_title="三日涨停形态统计",
        fetch=lambda: state_payload(pinning=True, unacked=[one_alert()]),
    )
    pin._first_read = False  # noqa: SLF001
    with caplog.at_level("WARNING"):
        pin.apply(pin._fetch())  # noqa: SLF001
    assert "托盘通知没发出去" in caplog.text


# --- 节奏与线程 --------------------------------------------------------------


def test_poll_seconds_follow_the_backend():
    state = FakeState(window=FakeWindow(), icon=FakeIcon())
    pin = WatchPin(state, base_url=state.url, base_title="标题", fetch=lambda: {})
    assert pin._poll_seconds() == DEFAULT_POLL_SECONDS  # noqa: SLF001
    pin.apply(state_payload(poll_seconds=5))
    assert pin._poll_seconds() == 5.0  # noqa: SLF001
    pin.apply(state_payload(poll_seconds=999))
    assert pin._poll_seconds() == MAX_POLL_SECONDS  # noqa: SLF001
    pin.apply(state_payload(poll_seconds=0.2))
    assert pin._poll_seconds() == MIN_POLL_SECONDS  # noqa: SLF001
    pin.apply(state_payload(poll_seconds=None))
    assert pin._poll_seconds() == MIN_POLL_SECONDS  # noqa: SLF001 - 坏值不改数
    pin.apply(state_payload(poll_seconds="4"))
    assert pin._poll_seconds() == 4.0  # noqa: SLF001


def test_thread_reads_once_and_exits_on_stop_event():
    window = FakeWindow()
    state = FakeState(window=window, icon=FakeIcon())

    def fetch() -> dict:
        pin.stop_event.set()
        return state_payload(pinning=True, unacked=[one_alert()])

    pin = WatchPin(state, base_url=state.url, base_title="标题", fetch=fetch)
    pin.start()
    pin.thread.join(timeout=5)
    assert pin.reads == 1
    assert window.pin_log == [True]
    pin.stop()
    assert pin.thread.is_alive() is False


def test_thread_exits_when_the_endpoint_never_answers():
    """真实读数（端口没人听）：线程只能一轮轮记失败，不能把进程带崩。"""
    state = FakeState(window=FakeWindow(), icon=FakeIcon())
    pin = WatchPin(state, base_url="http://127.0.0.1:9/", base_title="标题")
    pin.start()
    time.sleep(0.3)
    assert pin.thread is not None and pin.thread.is_alive()
    assert pin.reads == 0
    pin.stop()
    assert pin.thread.is_alive() is False


def test_stop_restores_pin_and_title():
    window = FakeWindow()
    state = FakeState(window=window, icon=FakeIcon())
    pin = WatchPin(
        state, base_url=state.url, base_title="三日涨停形态统计",
        fetch=lambda: state_payload(pinning=True, unacked=[one_alert()]),
    )
    pin.apply(pin._fetch())  # noqa: SLF001
    pin.stop()
    assert window.pin_log == [True, False]
    assert window.title_log[-1] == "三日涨停形态统计"
    assert state.quitting is False


def test_stop_is_idempotent():
    window = FakeWindow()
    state = FakeState(window=window, icon=FakeIcon())
    pin = WatchPin(state, base_url=state.url, base_title="标题", fetch=lambda: {})
    pin.start()
    pin.stop()
    pin.stop()
    assert window.pin_log == []


# --- 与启动器的接线 ----------------------------------------------------------


def test_launchers_state_has_a_slot_for_the_pin():
    from desktop.launcher import AppState, stop_watch_pin

    state = AppState()
    assert state.watch_pin is None
    stop_watch_pin(state)  # 没起过也不能炸
    assert state.watch_pin is None


def test_start_and_stop_watch_pin_from_launcher():
    from desktop.launcher import APP_TITLE, AppState, WatchPin, start_watch_pin, stop_watch_pin

    state = AppState()
    state.url = "http://127.0.0.1:9/"
    state.window = FakeWindow()
    state.icon = FakeIcon()
    start_watch_pin(state)
    pin = state.watch_pin
    assert isinstance(pin, WatchPin)
    assert pin.base_title == APP_TITLE
    assert pin.base_url == "http://127.0.0.1:9"
    assert pin.thread is not None and pin.thread.is_alive()
    stop_watch_pin(state)
    assert state.watch_pin is None
    assert pin.thread.is_alive() is False


def test_request_quit_releases_the_pin_before_destroying_the_window():
    """退出顺序是硬的：窗口销毁后就没人能取消置顶了。"""
    from desktop.launcher import AppState, request_quit, stop_watch_pin

    window = FakeWindow()
    icon = FakeIcon()
    state = AppState()
    state.window = window
    state.icon = icon
    pin = WatchPin(
        state, base_url="http://127.0.0.1:9/", base_title="三日涨停形态统计",
        fetch=lambda: state_payload(pinning=True, unacked=[one_alert()]),
    )
    pin.apply(pin._fetch())  # noqa: SLF001
    state.watch_pin = pin
    request_quit(state)
    assert window.pin_log == [True, False]
    assert window.title_log[-1] == "三日涨停形态统计"
    assert window.destroyed is True
    assert icon.stopped is True
    assert state.quitting is True
    assert state.watch_pin is None
    stop_watch_pin(state)  # 再收一次也不炸


def test_watch_pin_only_starts_for_the_real_backend():
    """回退应用没有报警榜接口：起了就是每 3 秒一条读失败，日志看着像行情坏了。"""
    from desktop.launcher import should_start_watch_pin

    assert should_start_watch_pin("backend") is True
    assert should_start_watch_pin("stub") is False


def test_the_fallback_app_really_lacks_the_endpoint():
    """上一条的理由要自己验一次：真去问回退应用，它确实答 404。"""
    from fastapi.testclient import TestClient

    from desktop.stub_app import create_app

    client = TestClient(create_app())
    assert client.get(ENDPOINT).status_code == 404


def test_endpoint_path_matches_where_the_backend_mounts_it():
    """桌面层拼的那个 URL 必须就是后端挂出来的那个：路由改名时这里先红，而不是运行时静默失联。"""
    routes = (ROOT / "backend" / "app" / "api" / "routes.py").read_text(encoding="utf-8")
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    suffix = ENDPOINT[len("/api") :] if ENDPOINT.startswith("/api") else ENDPOINT
    assert f'@router.get("{suffix}"' in routes
    assert 'include_router(router, prefix="/api")' in main
    assert f"/api{suffix}" == ENDPOINT
