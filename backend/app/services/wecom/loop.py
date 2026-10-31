"""后台盘中监控循环（Docker / 无界面时也在跑）：报警时段内刷新 AI 榜单与自选股行情、记录报警、推送企业微信。"""

from __future__ import annotations

from datetime import datetime

from backend.app.core.clock import now
from backend.app.core.database import session_scope
from backend.app.core.logging import get_logger

logger = get_logger("limit_pattern.wecom")
INTERVAL_SECONDS = 15


def tick(moment: datetime | None = None) -> dict:
    from backend.app.services import monitor_service, watchlist_service
    from backend.app.services.wecom import notify

    moment = moment or now()
    if not monitor_service.alert_phase(moment)["alerts_active"]:
        return {"active": False, "pushed": 0}
    pushed = 0
    with session_scope() as s:
        board = monitor_service.snapshot(s, moment)  # 同时写 IntradayWatchAlert，页面轮询照常能拿到
        hits = [x for x in board.get("items", []) if x.get("level") in ("warn", "alert")]
        pushed += notify.push_alerts(hits, "ranking", moment)
        try:
            wl = watchlist_service.snapshot(s, moment)
            hits = [x for x in wl.get("items", []) if x.get("level") in ("warn", "alert")]
            pushed += notify.push_alerts(hits, "watchlist", moment)
        except Exception as exc:  # noqa: BLE001
            logger.warning("自选股监控这一轮没完成：%s", exc)
    return {"active": True, "pushed": pushed}


def safe_tick() -> None:
    try:
        tick()
    except Exception:  # noqa: BLE001
        logger.exception("盘中监控循环出错")
