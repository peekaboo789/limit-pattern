"""盘中监控的轮次编排：榜前 N 条（默认 50）每 2 秒随请求重算，全市场每 120 秒一轮。

需求原话是「实时监控时同时看相似度最高的 50 条，每两秒刷新一次」。这两件事成本差两个
数量级，所以分开做：

* **榜前 N 条重算**：取这 N 只的快照、按代码取历史、建窗口打分，实测不到一秒，
  放得进 2 秒的节奏，因此它跟着请求走——页面看到的就是刚取回来的价格。
* **全市场一轮**：四千多只都要建窗口打分，加上线上取价要十几二十秒。
  这个绝不能放进 HTTP 请求里，由后台线程每 120 秒跑一次，产出下一轮快刷的候选名单。

线程没人看就停（`IDLE_EXIT_SECONDS`）：一轮全市场要吃掉十几秒 CPU，
页面都关掉了还在转，等于替不存在的观众排练。

「按哪个指标挑这 N 只」由 `metric` 决定，「N 等于几」由页面决定。两者改的都是**成员**，
不是只改显示顺序：所以每一轮全市场要把全量打分结果留在内存里（`full_rows`），
换口径或换条数时立刻从这一份重选榜前 N，不用等下一轮 120 秒。快刷只重算这 N 只的价格与形态。

不在交易时段时全市场一轮走 `settled` 回放：盘中快照在收盘后只会给当天收盘价，
为此每两分钟打一次行情源没有意义，而页面上照样要说清这一榜是回放的。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable

from backend.app.core import clock
from backend.app.core.constants import (
    FAST_REFRESH_SECONDS,
    FULL_ROUND_SECONDS,
    IDLE_EXIT_SECONDS,
    REALTIME_TOP_N,
    REALTIME_TOP_N_MAX,
    REALTIME_TOP_N_MIN,
)
from backend.app.core.database import session_scope
from backend.app.core.logging import get_logger
from backend.app.services.intraday_capture import capture_header
from backend.app.services.realtime_service import (
    MODE_AUTO,
    MODE_SETTLED,
    scan,
)

logger = get_logger("realtime_top")

STATUS_WARMING = "warming"
STATUS_LIVE = "live"
STATUS_UNAVAILABLE = "unavailable"


def _score_of(row: dict) -> float:
    return float(row.get("score") or 0.0)


def _gap_of(row: dict) -> float:
    """还差多少到涨停价。取不到的排到最后，但不等于它不差。"""
    value = row.get("to_limit_percent")
    return 999.0 if value is None else float(value)


RANK_METRICS: dict[str, dict[str, Any]] = {
    "score": {"key": "score", "label": "综合相似度", "value": _score_of},
    "shape": {"key": "shape", "label": "形态", "value": lambda row: float(row.get("shape_score") or 0.0)},
    "volume": {"key": "volume", "label": "成交量", "value": lambda row: float(row.get("volume_score") or 0.0)},
    "volume_pct": {"key": "volume_pct", "label": "量比", "value": lambda row: float(row.get("volume_pct_score") or 0.0)},
    "gap": {"key": "gap", "label": "距涨停价", "value": _gap_of, "ascending": True},
}
DEFAULT_METRIC = "score"


def metric_labels() -> list[dict[str, str]]:
    return [{"key": item["key"], "label": item["label"]} for item in RANK_METRICS.values()]


def _rank_rows(rows: list[dict], top_n: int, metric: str) -> list[dict]:
    """按口径取前 `top_n`。

    并列时先比综合分、再比代码：只按指标排的话，同一轮里分数相同的股票顺序会随遍历顺序抖，
    2 秒一刷的榜上就会看到两行互相窜位。
    """
    item = RANK_METRICS.get(metric) or RANK_METRICS[DEFAULT_METRIC]
    value: Callable[[dict], float] = item["value"]
    if item.get("ascending"):
        return sorted(rows, key=lambda row: (value(row), -_score_of(row), row["code"]))[:top_n]
    return sorted(rows, key=lambda row: (-value(row), -_score_of(row), row["code"]))[:top_n]


def _board(result: dict, top_n: int, metric: str) -> dict:
    """两榜合并按 `metric` 取前 `top_n`，再按**当前**涨停状态放回两榜。

    一只股票盘中被封住，它就从「尚未涨停」挪到「已涨停」，而不是从榜上消失。
    """
    merged = _rank_rows(result["rows"], top_n, metric)
    board = {key: value for key, value in result.items() if key != "rows"}
    board["limit_up_rows"] = [row for row in merged if row["is_limit_up"]]
    board["pending_rows"] = [row for row in merged if not row["is_limit_up"]]
    board["row_count"] = len(merged)
    return board


class RealtimeTracker:
    """一个进程内的监控轮次调度器。

    状态全在这份单例里：`top()` 既是读取入口也是「有人在看」的信号，
    全市场一轮只由后台线程跑，快刷只由请求线程跑，两边靠 `_state_lock` 交接。
    """

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        #: 非阻塞抢占：同一时刻只允许一个请求跑快刷，其余直接拿上一份算好的结果
        self._fast_busy = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_read_at = 0.0

        self.leader_codes: list[str] = []
        self.live_board: dict[str, Any] | None = None
        self.full_rows: list[dict] = []
        self.full_market: dict[str, Any] | None = None
        self.full_round_at: float | None = None
        #: 上一次**尝试**全市场一轮的时刻，成功与否都算。节流必须看这个而不是
        #: `full_round_at`：后者只在成功时更新，一轮失败后它是 None，判据于是恒真，
        #: 等于「一失败就立刻再来一轮」。
        self.last_full_attempt_at: float | None = None
        self.fast_round_at: float | None = None
        self.full_rounds = 0
        self.fast_rounds = 0
        self.metric = DEFAULT_METRIC
        self.top_n = REALTIME_TOP_N
        self.error = ""
        self.last_result: dict[str, Any] = {}

    # --- 对外 --------------------------------------------------------------

    def top(self, metric: str | None = None, top_n: int | None = None) -> dict[str, Any]:
        """给页面的一份榜：能算就现算，算不动就说清是哪一步不行。"""
        self._last_read_at = time.monotonic()
        self._ensure_thread()
        if (metric is not None and metric != self.metric) or (top_n is not None and top_n != self.top_n):
            self._switch_selection(metric, top_n)
        if not self.leader_codes:
            return self._payload(STATUS_WARMING)
        if self._fast_busy.acquire(blocking=False):
            try:
                self._run_fast_round()
            finally:
                self._fast_busy.release()
        state = self._read_state()
        return self._payload(STATUS_UNAVAILABLE if (self.error and not state["live"]) else STATUS_LIVE)

    def snapshot(self, *, metric: str | None = None, top_n: int | None = None) -> dict[str, Any]:
        """给「每日 AI 固定榜备输入」用的一份榜：按指定口径从上一轮全市场的打分结果里重选。

        不重新取价——那样「固定榜的输入」和「用户屏幕上那份相似榜」会长成两套打分结果。
        上一轮还没跑出来时才当场同步跑一轮：调用这条路的要么是收盘后的定时任务，
        要么是刚点了按钮的人，都不是 2 秒一次的刷榜请求。

        `metric` / `metric_label` 塞进返回值：证据行要写上「这一份是按什么口径选出来的」，
        否则落库的那 100 行不知道自己是从哪把尺子上取下来的。
        """
        key = metric if metric in RANK_METRICS else DEFAULT_METRIC
        limit = max(REALTIME_TOP_N_MIN, min(REALTIME_TOP_N_MAX, int(top_n or self.top_n)))
        with self._state_lock:
            rows = list(self.full_rows)
            result = dict(self.last_result)
        if not rows:
            self._run_full_round()
            with self._state_lock:
                rows = list(self.full_rows)
                result = dict(self.last_result)
        if not rows:
            raise RuntimeError(f"全市场一轮没有跑出任何打分结果：{self.error or '未知原因'}")
        board = _board({**result, "rows": rows}, limit, key)
        board["metric"] = key
        board["metric_label"] = RANK_METRICS[key]["label"]
        board["top_n"] = limit
        return board

    def status(self) -> dict[str, Any]:
        """不触发扫描的状态，给 `/api/realtime/status` 用。

        `phase` / `window_mode` / `morning_freeze` 是窗口口径的自检：榜还没出来时
        页面照样说得出「形态窗口最后一天是哪根线、上午的半天线留住了没有」，
        这三个数只能来自时钟与磁盘，不该要跑一轮扫描才知道。
        """
        today = clock.today()
        board = self.live_board or {}
        if board.get("trade_date"):
            try:
                today = datetime.strptime(str(board["trade_date"]), "%Y-%m-%d").date()
            except ValueError:
                pass
        return {
            "status": STATUS_LIVE if self.live_board else (STATUS_UNAVAILABLE if self.error else STATUS_WARMING),
            "top_n": self.top_n,
            "top_n_default": REALTIME_TOP_N,
            "top_n_min": REALTIME_TOP_N_MIN,
            "top_n_max": REALTIME_TOP_N_MAX,
            "refresh_seconds": FAST_REFRESH_SECONDS,
            "full_round_seconds": FULL_ROUND_SECONDS,
            "idle_exit_seconds": IDLE_EXIT_SECONDS,
            "metric": self.metric,
            "metrics": metric_labels(),
            "tracker_running": bool(self._thread and self._thread.is_alive()),
            "leader_count": len(self.leader_codes),
            "full_rounds": self.full_rounds,
            "fast_rounds": self.fast_rounds,
            "full_rows": len(self.full_rows),
            "phase": clock.session_phase(),
            "in_session": clock.in_session(),
            "source": board.get("source", ""),
            "window_mode": board.get("window_mode", ""),
            "morning_freeze": capture_header(today),
            "error": self.error,
            "trade_date": str(board.get("trade_date") or ""),
            "base_date": str(board.get("base_date") or ""),
            "pool_dates": board.get("pool_dates") or [],
            "pool_size": int(board.get("pool_size") or 0),
            "note": str(board.get("note") or ""),
            "disclaimer": str(board.get("disclaimer") or ""),
        }

    def reset(self) -> None:
        """测试收尾：把状态清空，线程留着（它是 daemon，进程退出自然带走）。"""
        with self._state_lock:
            self.leader_codes = []
            self.live_board = None
            self.full_rows = []
            self.full_market = None
            self.full_round_at = None
            self.last_full_attempt_at = None
            self.fast_round_at = None
            self.full_rounds = 0
            self.fast_rounds = 0
            self.error = ""
            self.last_result = {}
            self.metric = DEFAULT_METRIC
            self.top_n = REALTIME_TOP_N

    def run_full_round_now(self) -> None:
        """手动跑一轮全市场，给测试和「同步完立刻刷一次」用。"""
        self._run_full_round()

    def stop(self) -> None:
        """把「没人看」的信号做成一次读超时，线程自己在下一轮检查时退出。"""
        self._last_read_at = 0.0

    # --- 线程 --------------------------------------------------------------

    def _ensure_thread(self) -> None:
        with self._thread_lock:
            self._last_read_at = time.monotonic()
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="lp-realtime", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        logger.info(
            "实时监控线程启动：榜前 %s 条每 %s 秒随请求重算，全市场每 %s 秒一轮",
            self.top_n,
            FAST_REFRESH_SECONDS,
            FULL_ROUND_SECONDS,
        )
        try:
            while True:
                idle_for = time.monotonic() - self._last_read_at
                if idle_for > IDLE_EXIT_SECONDS:
                    logger.info("实时监控 %s 秒内无人读取，线程退出", round(idle_for))
                    return
                if time.monotonic() - (self.last_full_attempt_at or 0.0) >= FULL_ROUND_SECONDS:
                    self._run_full_round()
                time.sleep(0.5)
        except Exception as exc:  # pragma: no cover - 线程绝不能静默死掉
            logger.exception("实时监控线程异常退出：%s", exc)
            with self._state_lock:
                self.error = f"监控线程异常退出：{exc}"

    # --- 轮次 --------------------------------------------------------------

    def _mode(self) -> str:
        return MODE_AUTO if clock.in_session() else MODE_SETTLED

    def _run_full_round(self) -> None:
        started = time.monotonic()
        with self._state_lock:
            self.last_full_attempt_at = started
        mode = self._mode()
        try:
            with session_scope() as session:
                result = scan(session, mode=mode)
        except Exception as exc:  # noqa: BLE001 - 后台轮次的失败必须变成状态里的一句话
            with self._state_lock:
                self.error = str(exc) or type(exc).__name__
            logger.warning("全市场一轮失败：%s", self.error)
            return
        board = _board(result, self.top_n, self.metric)
        leaders = [row["code"] for row in board["limit_up_rows"] + board["pending_rows"]]
        with self._state_lock:
            self.leader_codes = leaders
            self.full_rows = list(result["rows"])
            self.last_result = {key: value for key, value in result.items() if key != "rows"}
            self.full_market = {
                "scanned": len(result["rows"]),
                "limit_up_count": result["limit_up_count"],
                "pending_count": result["pending_count"],
                "skipped_count": result["skipped_count"],
            }
            self.full_round_at = started
            self.full_rounds += 1
            self.error = ""
            self.live_board = board
        logger.info(
            "全市场一轮完成：%s 只已涨停 / %s 只尚未涨停，跳过 %s 只，榜前 %s 已换，耗时 %ss",
            result["limit_up_count"],
            result["pending_count"],
            result["skipped_count"],
            len(leaders),
            round(time.monotonic() - started, 1),
        )

    def _run_fast_round(self) -> None:
        with self._state_lock:
            codes = list(self.leader_codes)
        started = time.monotonic()
        try:
            with session_scope() as session:
                result = scan(session, mode=self._mode(), codes=codes)
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self.error = str(exc) or type(exc).__name__
            return
        board = _board(result, self.top_n, self.metric)
        with self._state_lock:
            self.live_board = board
            self.fast_round_at = started
            self.fast_rounds += 1
            self.error = ""
        spent = time.monotonic() - started
        if spent > FAST_REFRESH_SECONDS:
            logger.warning("榜前 %s 只重算用了 %ss，超过 %s 秒的刷新节奏", len(codes), round(spent, 1), FAST_REFRESH_SECONDS)

    # --- 状态读取 ----------------------------------------------------------

    def _switch_selection(self, metric: str | None, top_n: int | None) -> None:
        """换口径或换条数 = 换成员：用上一轮全市场留下的打分结果立刻重选榜前 N。

        只改排序不改成员是骗人的榜——「量能分最高的那几只」本来就可能没进按综合分选出的
        前 N，重排现有 N 行永远看不见它们。上一轮还没跑出来时只能先记下选择，
        榜要等第一轮全市场才有成员可选。
        """
        with self._state_lock:
            if metric is not None and metric in RANK_METRICS:
                self.metric = metric
            if top_n is not None:
                self.top_n = max(REALTIME_TOP_N_MIN, min(REALTIME_TOP_N_MAX, int(top_n)))
            rows = list(self.full_rows)
            result = dict(self.last_result)
            #: 旧榜是按另一个选择选出来的，不能当新榜接着显示；紧接着的快刷会用新名单
            #: 重算一份出来，重算失败就照实显示「不可用」，不拿旧选择的榜冒充。
            self.live_board = None
            if not rows:
                return
            rebuilt = _board({**result, "rows": rows}, self.top_n, self.metric)
            self.leader_codes = [row["code"] for row in rebuilt["limit_up_rows"] + rebuilt["pending_rows"]]
            self.live_board = rebuilt

    def _read_state(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "live": dict(self.live_board) if self.live_board else None,
                "market": dict(self.full_market) if self.full_market else None,
                "leader_count": len(self.leader_codes),
                "metric": self.metric,
                "top_n": self.top_n,
                "full_round_at": self.full_round_at,
                "fast_round_at": self.fast_round_at,
                "full_rounds": self.full_rounds,
                "fast_rounds": self.fast_rounds,
                "error": self.error,
            }

    def _payload(self, status: str) -> dict[str, Any]:
        state = self._read_state()
        now = time.monotonic()
        metric = RANK_METRICS.get(state["metric"]) or RANK_METRICS[DEFAULT_METRIC]
        payload: dict[str, Any] = {
            "status": status,
            "top_n": state["top_n"],
            "top_n_default": REALTIME_TOP_N,
            "top_n_min": REALTIME_TOP_N_MIN,
            "top_n_max": REALTIME_TOP_N_MAX,
            "refresh_seconds": FAST_REFRESH_SECONDS,
            "full_round_seconds": FULL_ROUND_SECONDS,
            "metric": metric["key"],
            "metric_label": metric["label"],
            "metrics": metric_labels(),
            "leader_count": state["leader_count"],
            "full_rounds": state["full_rounds"],
            "fast_rounds": state["fast_rounds"],
            "full_round_at": _wall_clock(state["full_round_at"], now),
            "full_round_seconds_ago": _ago(state["full_round_at"], now),
            "fast_round_at": _wall_clock(state["fast_round_at"], now),
            "fast_round_seconds_ago": _ago(state["fast_round_at"], now),
            "market": state["market"],
            "error": state["error"],
        }
        live = state["live"]
        if live is None:
            payload["snapshot"] = None
        else:
            payload["snapshot"] = live
            payload["row_count"] = live.get("row_count", 0)
        return payload


def _ago(mark: float | None, now: float) -> float | None:
    return None if mark is None else round(now - mark, 1)


def _wall_clock(mark: float | None, now: float) -> str | None:
    """把 `time.monotonic()` 的读数换算成墙上时间，页面好显示「上一轮是几点」。"""
    if mark is None:
        return None
    return datetime.fromtimestamp(time.time() - (now - mark)).isoformat(timespec="seconds")


#: 进程内单例：一个后端进程一个监控循环
tracker = RealtimeTracker()
