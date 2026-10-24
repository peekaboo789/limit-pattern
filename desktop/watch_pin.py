"""桌面层的「涨幅报警置顶」：读后端那一份账，把窗口摞到最前面、改窗口标题、往托盘发通知。

为什么这一层必须存在（用户 2026-10-08 的四条拍板之一）：浏览器那一层的报警只在页面开着的时候敲得响，
而窗口收进托盘之后用户就不在这儿了——只有进程级的置顶与托盘通知才管得到「没看着页面的那几分钟」。

三条纪律跟着整个系统一贯的口径：

* **一个数都不自己算**。判据、阈值、档位、还剩几条全在后端；这一层只读
  `GET /api/realtime/intraday-watch` 回包里的 `pinning / pending_pin_codes / unacked / tier_label`。
  桌面层要是再算一遍「涨过 6%」，两边迟早对不上。
* **读不到维持上一份**。一次 HTTP 失败不等于「没有报警了」——把置顶撤掉是把「不知道」
  当成「知道没有」，用户丢的是唯一那条真在摞着的窗口。
* **托盘通知只在真的新报警上响**。判据是「这一条我在这个进程里通知过没有」（不是「今天报过没有」，
  那本账在库里）；开机第一遍读到的旧账只置顶、不发通知，否则每次启动都把昨天的没确认挨个弹一遍。

顺带一件不显眼的事：这条链路按后端 `poll_seconds`（当前 3 秒）读一次状态，而读一次就是
`keepalive` 一下监控线程，所以**页面关着、窗口收进托盘时后端的采样也不会停**——这正是报警榜要的效果。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger("limitpattern.watch_pin")

#: 这一档接口挂在同一个服务端口下；桌面层拼绝对 URL，页面用的是相对路径
ENDPOINT = "/api/realtime/intraday-watch"
DEFAULT_POLL_SECONDS = 3.0
MIN_POLL_SECONDS = 1.0
MAX_POLL_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 2.0
#: 连续读失败多少条才写一次日志：这一层每 3 秒读一次，一次网络抖就刷 20 行没意义
LOG_EVERY_FAILURES = 5


def alert_key(item: dict[str, Any]) -> str:
    """一条报警在**本进程**里的身份：交易日 + 代码 + 档位。

    带上交易日是因为「每天每档只报一次」的账是按天算的；不带的话，
    跨零点还活着的进程会把今天的新报警认成昨天那条，从此不再通知。
    """
    return f"{item.get('trade_date')}|{item.get('stock_code')}|{item.get('tier')}"


def alert_text(item: dict[str, Any]) -> str:
    """托盘里那一句话：只用后端给的字段拼，前端/桌面都不补一个判据数字。"""
    name = str(item.get("stock_name") or item.get("stock_code") or "")
    code = str(item.get("stock_code") or "")
    label = str(item.get("tier_label") or "涨幅报警")
    percent = item.get("percent")
    tail = f"：今日 {float(percent):.2f}%" if isinstance(percent, (int, float)) else ""
    return f"{name}（{code}）{label}{tail}"


def title_of(base_title: str, unacked_count: int) -> str:
    """窗口标题：有待确认就把条数顶在最前面，没有就还原。"""
    if unacked_count <= 0:
        return base_title
    return f"⚠ 涨幅报警 {unacked_count} 条待确认 · {base_title}"


class WatchPin:
    """一条常驻线程：读状态 → 置顶/取消置顶 → 改标题 → 托盘通知。

    `state` 只要三样东西（`AppState` 都有，测试里用一个假对象就够）：
    `window`（pywebview 窗口，可能为 None，浏览器模式下就没有）、
    `icon`（pystray 托盘图标，可能为 None）、`quitting`（退出标志）。
    """

    def __init__(
        self,
        state: Any,
        *,
        base_url: str,
        base_title: str,
        fetch: Callable[[], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state = state
        self.base_url = base_url.rstrip("/")
        self.base_title = base_title
        self._fetch = fetch or self._read_endpoint
        self._sleep = sleep
        self.stop_event = threading.Event()
        self._pinned = False
        self._title = base_title
        self._notified: set[str] = set()
        self._first_read = True
        self._failures = 0
        self.reads = 0
        #: 轮询节奏，初值是猜的；第一次读成功后换成后端 `poll_seconds` 报的那个数
        self._seconds = DEFAULT_POLL_SECONDS
        self.thread: threading.Thread | None = None

    # --- 启动与停止 --------------------------------------------------------

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, name="limitpattern-watch-pin", daemon=True)
        self.thread.start()
        logger.info("涨幅报警置顶线程已启动：%s%s", self.base_url, ENDPOINT)

    def stop(self) -> None:
        """收尾必须把置顶与标题交回去：用户退出程序之后桌面不该还压着一层 TOPMOST。"""
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        self.restore()

    def run(self) -> None:
        while not self.stop_event.is_set() and not getattr(self.state, "quitting", False):
            seconds = self._poll_seconds()
            try:
                payload = self._fetch()
            except Exception as error:  # noqa: BLE001 - 读不到只许维持上一份
                self._failures += 1
                if self._failures == 1 or self._failures % LOG_EVERY_FAILURES == 0:
                    logger.warning(
                        "读涨幅报警榜状态失败（连续第 %s 次）：%s；维持上一份置顶，不撤",
                        self._failures,
                        error,
                    )
                # 读失败**不撤置顶**：那等于把「不知道有没有」当成「没有了」
            else:
                self._failures = 0
                self.reads += 1
                self.apply(payload)
            if self.stop_event.wait(seconds):
                break
            if getattr(self.state, "quitting", False):
                break

    # --- 判据之外的一切都在后端：这里只做「读到的那份落到窗口上」 ------------

    def apply(self, payload: dict[str, Any]) -> None:
        self.set_poll_seconds(payload.get("poll_seconds"))
        unacked = payload.get("unacked") or []
        pinning = bool(payload.get("pinning"))
        self._set_pinned(pinning)
        self._set_title(title_of(self.base_title, len(unacked)))
        trade_date = str(payload.get("trade_date") or "")
        if self._first_read:
            #: 开机第一遍读到的旧账只置顶、不发通知——否则每次启动都把没确认的那几条挨个弹一遍
            for item in unacked:
                self._notified.add(alert_key(item))
            self._first_read = False
            return
        for item in unacked:
            key = alert_key(item)
            if key in self._notified:
                continue
            self._notified.add(key)
            self._notify(alert_text(item))
        self._forget_other_days(trade_date)

    def _forget_other_days(self, trade_date: str) -> None:
        """清掉不属于这一天的通知记录，别让常驻进程的集合一直长。

        留着昨天的键没有意义：库里的去重键按天算，昨天那几条今天不会再来，
        而跨零点还活着的进程必须把今天当成新的一天重新报一遍。
        """
        if not trade_date:
            return
        mark = trade_date + "|"
        for key in [item for item in self._notified if not item.startswith(mark)]:
            self._notified.discard(key)

    def _set_pinned(self, pinning: bool) -> None:
        if pinning == self._pinned:
            return
        window = getattr(self.state, "window", None)
        if window is None:
            #: 浏览器模式或窗口还没建好：这一层什么都不做，等下一次读
            return
        try:
            window.on_top = pinning
        except Exception as error:  # noqa: BLE001
            logger.warning("设置窗口置顶失败（%s）：%s", "摞上" if pinning else "放开", error)
            return
        self._pinned = pinning
        logger.info("窗口置顶已%s", "打开" if pinning else "取消")

    def _set_title(self, title: str) -> None:
        if title == self._title:
            return
        window = getattr(self.state, "window", None)
        if window is None:
            return
        try:
            window.set_title(title)
        except Exception as error:  # noqa: BLE001
            logger.warning("改窗口标题失败：%s", error)
            return
        self._title = title

    def _notify(self, message: str) -> None:
        icon = getattr(self.state, "icon", None)
        if icon is None:
            #: 没有托盘时这句话必须落在日志里，不能假装通知发出去了
            logger.info("涨幅报警（无托盘，只记日志）：%s", message)
            return
        try:
            icon.notify(message, self.base_title)
        except Exception as error:  # noqa: BLE001
            logger.warning("托盘通知没发出去（%s）：%s", message, error)

    def restore(self) -> None:
        """退出前把窗口恢复原样：撤置顶、标题还原。"""
        window = getattr(self.state, "window", None)
        if window is None:
            return
        if self._pinned:
            try:
                window.on_top = False
            except Exception:  # noqa: BLE001
                logger.exception("退出时取消置顶失败")
            self._pinned = False
        if self._title != self.base_title:
            try:
                window.set_title(self.base_title)
            except Exception:  # noqa: BLE001
                logger.exception("退出时还原标题失败")
            self._title = self.base_title

    # --- 读数：只读，一次网关都不敲 ----------------------------------------

    def _read_endpoint(self) -> dict[str, Any]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(
            f"{self.base_url}{ENDPOINT}", headers={"Accept": "application/json"}
        )
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("回包不是 JSON 对象")
        return data

    def _poll_seconds(self) -> float:
        """节奏跟着后端那个常量走：`apply()` 每次读回来都换成它报的那个数。"""
        return self._seconds

    def set_poll_seconds(self, seconds: Any) -> None:
        """对齐本层轮询间隔。后端那个值是唯一的数，这里只兜住它被改坏的情况。"""
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        self._seconds = max(MIN_POLL_SECONDS, min(MAX_POLL_SECONDS, value))
