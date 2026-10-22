"""明细只留最近若干个交易日。每日总结不在清理范围内。"""

from __future__ import annotations

from backend.app.core.clock import SHANGHAI
from backend.app.core.config import retention_days, stamp
from backend.app.core.database import session_scope
from backend.app.models.tables import (
    CalcBar,
    DailyFeature,
    DailyQuote,
    DataQualityError,
    LimitUpPattern,
    PatternMatch,
    SkippedRecord,
    TradingDay,
)
from datetime import datetime


def quote_dates(session, as_of=None) -> list:
    query = session.query(DailyQuote.trade_date).distinct()
    if as_of is not None:
        query = query.filter(DailyQuote.trade_date <= as_of)
    return [row[0] for row in query.order_by(DailyQuote.trade_date)]


def purge_expired() -> dict:
    """删掉保留窗口之前的行情和标签。总结表和 summaries/ 文件不动。"""
    keep = retention_days()
    with session_scope() as session:
        days = quote_dates(session)
        if not days:
            deleted = {"daily_quotes": 0, "calc_bars": 0}
            kept = []
            cutoff = None
        elif len(days) <= keep:
            # 正式行情已经不多于保留天数。更早的补拉 K 线仍然要删掉。
            cutoff = days[0]
            deleted = {
                "daily_quotes": 0,
                "calc_bars": session.query(CalcBar).filter(CalcBar.trade_date < cutoff).delete(synchronize_session=False),
            }
            kept = [day.isoformat() for day in days]
        else:
            cutoff = days[-keep]
            deleted = {
                "daily_quotes": session.query(DailyQuote).filter(DailyQuote.trade_date < cutoff).delete(synchronize_session=False),
                "calc_bars": session.query(CalcBar).filter(CalcBar.trade_date < cutoff).delete(synchronize_session=False),
                "daily_features": session.query(DailyFeature).filter(DailyFeature.trade_date < cutoff).delete(synchronize_session=False),
                "limit_up_patterns": session.query(LimitUpPattern).filter(LimitUpPattern.limit_up_date < cutoff).delete(synchronize_session=False),
                "pattern_matches": session.query(PatternMatch).filter(PatternMatch.target_date < cutoff).delete(synchronize_session=False),
                "skipped_records": session.query(SkippedRecord).filter(SkippedRecord.trade_date < cutoff).delete(synchronize_session=False),
                "data_quality_errors": session.query(DataQualityError).filter(DataQualityError.trade_date < cutoff).delete(synchronize_session=False),
                "trading_days": session.query(TradingDay).filter(TradingDay.trade_date < cutoff).delete(synchronize_session=False),
            }
            kept = [day.isoformat() for day in days if day >= cutoff]
    from backend.app.services.history_backfill import clear_calc_memory

    clear_calc_memory()
    moment = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    stamp("last_cleanup_at", moment)
    return {
        "retention_trading_days": keep,
        "cutoff": None if cutoff is None else cutoff.isoformat(),
        "kept_dates": kept,
        "deleted": deleted,
        "last_cleanup_at": moment,
    }
