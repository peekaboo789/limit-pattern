"""`intraday_watch_alerts` 的读写与保留（盘中涨幅报警的账）。

三条取向与另两张 AI 表一致：

1. **函数一律收 `session`**，不自己 `session_scope()`：测试夹具把全局会话换成内存库时，
   另借一条全局引擎的连接会把报警写进开发机那份真库。
2. **去重靠数据库的唯一约束，不靠调用方记得住**。`record_alert()` 先查后插只是省一次异常，
   真正的判据是 `uq_watch_alert`：撞到它就叫「这一档今天已经报过」，返回 None 而不是
   一条新报警。
3. **报警账是派生数据**。它记的是「几点敲过用户一次」，原始行情才是红线，
   所以 `ALERT_RETENTION_DAYS` 可以放心裁——裁掉只丢历史，不影响任何计算口径。

`acked_at` 是这张表唯一的「用户动作」列：空串表示还没确认，而**未确认的置顶档正是桌面版
该把窗口摞在最前面那些只**。所以解除置顶不是前端的布尔变量，是这里的一次写库：页面刷新、
窗口重开、服务重启之后，「还有几条没点知道了」仍然答得出来。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.constants import ALERT_RETENTION_DAYS
from backend.app.models.tables import IntradayWatchAlert

#: 第一档：当日累计涨幅到 3%，弹窗报警（只看涨幅，不看涨速）
TIER_ALERT = "alert_3pct"
#: 第二档：涨幅到 6% **且**近 3 分钟跳升够数，「直线上涨」——桌面版把窗口置顶
TIER_SPIKE = "spike_6pct"
TIERS: tuple[str, ...] = (TIER_ALERT, TIER_SPIKE)
TIER_LABELS: dict[str, str] = {
    TIER_ALERT: "涨过 3%（弹窗）",
    TIER_SPIKE: "涨过 6% 且在冲（置顶窗口）",
}

#: 落库时刻统一用定长 ISO 串：字典序就是时间序，裁剪按它比较才成立
_NOW = "%Y-%m-%dT%H:%M:%S"


def _now_iso() -> str:
    return datetime.now().strftime(_NOW)


def as_date(value: str | date | None) -> date | None:
    """报警的日期键：接口与线程都传 ISO 串，列本身是 Date，这里统一转一次。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def as_dict(row: IntradayWatchAlert) -> dict[str, Any]:
    """一条报警的对外形状。`acked_at` 空串就是「还没确认」，页面据此摆「知道了」那颗钮。"""
    return {
        "id": int(row.id),
        "trade_date": row.trade_date.isoformat() if row.trade_date else "",
        "stock_code": row.stock_code,
        #: 旧库里补进来的行没有这两列的值，读成空串而不是 None：页面显示「—」靠的是空串
        "stock_name": row.stock_name or "",
        "tier": row.tier,
        "tier_label": TIER_LABELS.get(row.tier, row.tier),
        #: 名次与分数是 nullable：读不出就是 None，页面显示「—」而不是 0 名 / 0 分
        "rank": None if row.rank is None else int(row.rank),
        "score": None if row.score is None else float(row.score),
        "percent": None if row.percent is None else float(row.percent),
        "rise_3min": None if row.rise_3min is None else float(row.rise_3min),
        "fired_at": row.fired_at or (
            row.created_at.strftime(_NOW) if row.created_at else ""
        ),
        "acked_at": row.acked_at or "",
    }


def record_alert(
    session: Session,
    *,
    trade_date: str | date,
    stock_code: str,
    stock_name: str,
    tier: str,
    percent: float | None,
    rank: int | None = None,
    score: float | None = None,
    rise_3min: float | None = None,
) -> dict[str, Any] | None:
    """记下「这一档今天第一次报」；已经报过就返回 None。

    返回 None 与返回一行的区别就是页面该不该敲这一响：**第一次**才响。
    已存在的那一条**原样留着**——它带的是当时那个价，用后来的数覆盖一次，
    这条报警就再也追不出「几点、百分之几报的」。
    """
    day = as_date(trade_date)
    if day is None or tier not in TIERS:
        return None
    existing = session.scalars(
        select(IntradayWatchAlert)
        .where(
            IntradayWatchAlert.trade_date == day,
            IntradayWatchAlert.stock_code == stock_code,
            IntradayWatchAlert.tier == tier,
        )
        .limit(1)
    ).first()
    if existing is not None:
        return None
    row = IntradayWatchAlert(
        trade_date=day,
        stock_code=stock_code,
        stock_name=stock_name,
        tier=tier,
        rank=rank,
        score=score,
        percent=None if percent is None else float(percent),
        rise_3min=rise_3min,
        fired_at=_now_iso(),
        acked_at="",
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        #: 与并发的那一次撞上了唯一约束：这不是故障，正是「每只每天每档一次」在生效。
        #: SQLite 上的 `IntegrityError` 会让当前事务里的这句插入连同未提交的东西一起作废，
        #: 所以调用方（监控轮次）为每条报警单开一条 session：撞了就丢这一条，
        #: 同一轮里另外几只已经落好的报警不受牵连。
        session.rollback()
        return None
    prune(session)
    return as_dict(row)


def alerts_of(session: Session, trade_date: str | date) -> list[dict[str, Any]]:
    """那一天报过哪些（同一份排序口径见 `_alert_order`，弹窗档在前、先触发的在前）。"""
    day = as_date(trade_date)
    if day is None:
        return []
    rows = session.scalars(
        select(IntradayWatchAlert)
        .where(IntradayWatchAlert.trade_date == day)
        .order_by(IntradayWatchAlert.tier, IntradayWatchAlert.fired_at, IntradayWatchAlert.id)
    ).all()
    return [as_dict(row) for row in rows]


def pending_pin_codes(session: Session, trade_date: str | date) -> list[str]:
    """**还没确认**的置顶档有哪些股票（桌面版据此决定要不要把窗口摞在最前面）。

    只看 `TIER_SPIKE`：第一档「涨过 3%」是提示，不摞窗口——拍板的是
    「直线上涨超过 6% 才置顶」。按代码去重、稳定排序，接口每一轮给同一条名单。
    """
    day = as_date(trade_date)
    if day is None:
        return []
    rows = session.scalars(
        select(IntradayWatchAlert.stock_code)
        .where(
            IntradayWatchAlert.trade_date == day,
            IntradayWatchAlert.tier == TIER_SPIKE,
            IntradayWatchAlert.acked_at == "",
        )
        .order_by(IntradayWatchAlert.stock_code)
    ).all()
    return sorted({str(code) for code in rows})


def ack_alerts(
    session: Session,
    trade_date: str | date,
    *,
    stock_code: str | None = None,
    tier: str | None = None,
) -> int:
    """点「知道了」：把还没确认的那几条写上确认时刻，返回真的被确认掉的条数。

    `stock_code`/`tier` 不给就是「全部知道了」（浮层上那颗总按钮）。已经确认过的行
    不再重复写：`acked_at == ""` 是判据，重复点不会把确认时刻刷成新值，
    页面上「几点取消的置顶」才保得住。
    """
    day = as_date(trade_date)
    if day is None:
        return 0
    stmt = (
        update(IntradayWatchAlert)
        .where(
            IntradayWatchAlert.trade_date == day,
            IntradayWatchAlert.acked_at == "",
        )
        .values(acked_at=_now_iso())
    )
    if stock_code is not None:
        stmt = stmt.where(IntradayWatchAlert.stock_code == stock_code)
    if tier is not None:
        stmt = stmt.where(IntradayWatchAlert.tier == tier)
    result = session.execute(stmt)
    return int(result.rowcount or 0)


def prune(session: Session) -> int:
    """裁掉保留窗口外的报警，返回删掉的行数。

    写完顺手裁，不留「没人点就永远涨」的缝。`fired_at` 为空的旧行按 `created_at` 裁——
    这一列是后补的，不裁的话那一天的账会一直躺在表里没人管。
    """
    cutoff = (datetime.now() - timedelta(days=ALERT_RETENTION_DAYS)).strftime(_NOW)
    stale = datetime.now() - timedelta(days=ALERT_RETENTION_DAYS)
    result = session.execute(
        delete(IntradayWatchAlert).where(
            IntradayWatchAlert.fired_at != "",
            IntradayWatchAlert.fired_at < cutoff,
        )
    )
    removed = int(result.rowcount or 0)
    fallback = session.execute(
        delete(IntradayWatchAlert).where(
            IntradayWatchAlert.fired_at == "",
            IntradayWatchAlert.created_at < stale,
        )
    )
    return removed + int(fallback.rowcount or 0)


__all__ = [
    "TIER_ALERT",
    "TIER_LABELS",
    "TIER_SPIKE",
    "TIERS",
    "ack_alerts",
    "alerts_of",
    "as_date",
    "as_dict",
    "pending_pin_codes",
    "prune",
    "record_alert",
]
