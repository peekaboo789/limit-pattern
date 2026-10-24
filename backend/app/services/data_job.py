"""数据管理那四个重建任务：一个槽位、后台线程、进度可查。

为什么只放一个槽：这些活都在写 SQLite。老工程实测过「同步行情还没跑完就点重建样本」，
两个请求一起把写锁撞成 `database is locked`，两边都 500，用户完全不知道发生了什么。
现在的做法是第二个请求直接 409，并在消息里说清是谁在占用这个槽位。

为什么不阻塞请求：一轮全市场匹配扫描要给四千多只股票逐个构窗口再打分，
区间重建特征要读全库行情。这些不是能在一个 HTTP 请求里等完的活，
所以任务在后台线程跑，页面靠 `/api/data/status` 那一拍读进度——
「读数不靠重进页面」也要求进度是查得到的，不是只打在日志里。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from backend.app.core.clock import SHANGHAI
from backend.app.core.database import session_scope, wal_checkpoint
from backend.app.core.logging import get_logger

from datetime import datetime

logger = get_logger("data_job")

_LABELS: dict[str, str] = {
    "features": "重新计算特征",
    "patterns": "重建涨停样本",
    "scan": "全市场匹配扫描",
    "training": "构造训练集",
}

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "running": False,
    "kind": "",
    "label": "",
    "task_id": "",
    "started_at": "",
    "finished_at": "",
    "progress": 0,
    "step": "",
    "message": "",
    "error": "",
    "result": None,
}


class JobBusy(RuntimeError):
    """槽位被别人占着。接口层把它翻译成 409 并带上占用者的名字。"""


def current_job() -> str:
    with _LOCK:
        return str(_STATE["label"]) if _STATE["running"] else ""


def state() -> dict:
    with _LOCK:
        payload = {key: value for key, value in _STATE.items()}
    payload["labels"] = dict(_LABELS)
    return payload


def reset_state() -> None:
    """测试收尾用：不等那个 daemon 线程。"""
    with _LOCK:
        _STATE.update(
            running=False,
            kind="",
            label="",
            task_id="",
            progress=0,
            step="",
            message="",
            error="",
            result=None,
        )


def start(kind: str, *, dates: list[date], work: Callable[[Any, date, dict], None]) -> dict:
    """起一个任务。`work(session, day, accumulator)` 在自己的 session 里写库。

    `dates` 是这个任务要处理的交易日列表（一次扫描一天、一次区间重建可以是几十天）：
    列表为空时调用方就该当场报错，不该起一个跑完才发现无事可做的线程。
    `accumulator` 是跨天累加的那份账，任务跑完就是页面上看到的结果卡片。
    """
    label = _LABELS.get(kind, kind)
    if not dates:
        raise ValueError(f"{label}：没有要处理的交易日，先同步行情")
    with _LOCK:
        if _STATE["running"]:
            raise JobBusy(f"后端任务「{_STATE['label'] or _STATE['kind']}」正在运行，请等它跑完")
        _STATE.update(
            running=True,
            kind=kind,
            label=label,
            task_id=uuid.uuid4().hex[:12],
            started_at=datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            finished_at="",
            progress=0,
            step="",
            message=f"正在执行{label}（{len(dates)} 个交易日）",
            error="",
            result=None,
        )
        task_id = str(_STATE["task_id"])
    thread = threading.Thread(
        target=_run, args=(task_id, kind, label, dates, work), name=f"lp-{kind}", daemon=True
    )
    thread.start()
    return {"task_id": task_id, "label": label, "days_total": len(dates), **_public()}


def _run(task_id: str, kind: str, label: str, dates: list[date], work) -> None:
    accumulator: dict = {"days": [], "label": label}
    try:
        for index, day in enumerate(dates):
            _set(
                task_id,
                progress=int(index / len(dates) * 100),
                step=day.isoformat(),
                message=f"{label}：{day.isoformat()}（{index + 1}/{len(dates)}）",
            )
            with session_scope() as session:
                work(session, day, accumulator)
            accumulator["days"].append(day.isoformat())
        _set(task_id, progress=100, step="", message=f"{label}完成", result=accumulator)
        wal_checkpoint(f"{label}收尾")
        logger.info("%s 完成，覆盖 %s 个交易日", label, len(dates))
    except Exception as exc:  # noqa: BLE001 - 后台线程的异常要变成页面上的一句话
        logger.warning("%s 失败：%s", label, exc, exc_info=True)
        _set(task_id, message=f"{label}失败", error=str(exc), result=accumulator)
    finally:
        with _LOCK:
            if _STATE["task_id"] == task_id:
                _STATE["running"] = False
                _STATE["finished_at"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _set(task_id: str, **changes) -> None:
    with _LOCK:
        if _STATE["task_id"] != task_id:
            return
        _STATE.update(changes)


def _public() -> dict:
    return {
        "running": bool(_STATE["running"]),
        "progress": int(_STATE["progress"] or 0),
        "message": str(_STATE["message"]),
        "error": str(_STATE["error"]),
        "task_id": str(_STATE["task_id"]),
    }


__all__ = ["JobBusy", "current_job", "reset_state", "start", "state"]
