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


def _safe_summary() -> None:
    try:
        from backend.app.services.summary_service import ensure_daily_summary
        from backend.app.services.sync_service import status

        if status().get("running"):
            logger.info("同步还在进行，这一轮先不补写总结")
            return
        ensure_daily_summary()
    except Exception:
        logger.exception("定时总结失败")


def _safe_daily_rank_prepare() -> None:
    """上午 10:00 把「每日 AI 固定榜」的那 100 行输入备好。

    这一步只算本系统自己的数（相似度、特征、评分模型），一分钱不花、一次网关都不敲；
    花钱的那一次排名永远要用户在页面上点一下。备好的输入落 `status=prepared`。
    挑 10:00 而不是收盘后，是因为页面上那份榜读的就是当天上午的形态窗口——
    等到 17:10 才备，用户整个白天都点不动那颗花钱的按钮。

    非交易日会被「交易日必须是今天」那道闸挡回来（休市日行情源回的还是上一个交易日的价，
    照原样落库等于拿昨天的整张榜覆盖昨天的输入、顺手把已经排好的名次压回 `prepared`），
    那句原因写进日志。
    """
    try:
        from backend.app.core.database import session_scope
        from backend.app.services.ai_daily_rank_service import prepare_from_tracker

        with session_scope() as session:
            result = prepare_from_tracker(session)
        if not result.get("ok"):
            logger.info("每日 AI 固定榜没有备好输入：%s", result.get("error"))
    except Exception:
        logger.exception("每日 AI 固定榜备输入失败")


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
    scheduler.add_job(_safe_summary, "cron", hour=18, minute=10, id="daily-summary")
    scheduler.add_job(_safe_daily_rank_prepare, "cron", hour=10, minute=0, id="daily-rank-prepare")
    scheduler.start()
    _SCHEDULER = scheduler
    logger.info(
        "定时任务已启动：10:00 备每日 AI 固定榜的输入，17:10 同步并写总结，"
        "17:40 清理明细，18:10 补写缺失的总结"
    )


def scheduled_job_ids() -> list[str]:
    if _SCHEDULER is None:
        return []
    return [job.id for job in _SCHEDULER.get_jobs()]


def stop_scheduler() -> None:
    global _SCHEDULER
    if _SCHEDULER is not None:
        _SCHEDULER.shutdown(wait=False)
        _SCHEDULER = None
