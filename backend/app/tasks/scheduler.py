"""收盘后自动同步，并再跑一次明细清理。测试里用 LP_SCHEDULER=0 关掉。"""

from __future__ import annotations

import os

from backend.app.core.logging import get_logger
from backend.app.services.retention import purge_expired
from backend.app.services.sync_service import start_sync

logger = get_logger("scheduler")
_SCHEDULER = None


def _safe_sync() -> None:
    try:
        start_sync()
    except Exception:
        logger.exception("定时同步没有启动")


def _safe_purge() -> None:
    try:
        purge_expired()
    except Exception:
        logger.exception("定时清理失败")


def start_scheduler() -> None:
    global _SCHEDULER
    if os.environ.get("LP_SCHEDULER", "1") == "0":
        logger.info("LP_SCHEDULER=0，不启动定时任务")
        return
    if _SCHEDULER is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(_safe_sync, "cron", hour=17, minute=10, id="daily-sync")
    scheduler.add_job(_safe_purge, "cron", hour=17, minute=40, id="daily-purge")
    scheduler.start()
    _SCHEDULER = scheduler
    logger.info("定时任务已启动：17:10 同步，17:40 清理明细")


def stop_scheduler() -> None:
    global _SCHEDULER
    if _SCHEDULER is not None:
        _SCHEDULER.shutdown(wait=False)
        _SCHEDULER = None
