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
    MatchSummary,
    PatternMatch,
    SkippedRecord,
    StockTrainingRow,
    TradingDay,
)
from datetime import date, datetime, timedelta


def quote_dates(session, as_of=None) -> list:
    query = session.query(DailyQuote.trade_date).distinct()
    if as_of is not None:
        query = query.filter(DailyQuote.trade_date <= as_of)
    return [row[0] for row in query.order_by(DailyQuote.trade_date)]


def purge_excluded_boards() -> dict:
    """删掉配置里排除的板块。默认是科创板。启动和每次清理都会走这里。"""
    from backend.app.core.config import excluded_boards
    from backend.app.models.tables import Stock
    from backend.app.providers.markets import classify

    excluded = excluded_boards()
    if not excluded:
        return {"codes": 0}
    with session_scope() as session:
        codes: set[str] = set()
        for column in (
            DailyQuote.stock_code,
            CalcBar.stock_code,
            DailyFeature.stock_code,
            LimitUpPattern.stock_code,
            MatchSummary.target_code,
            StockTrainingRow.stock_code,
            SkippedRecord.stock_code,
            DataQualityError.stock_code,
        ):
            codes.update(row[0] for row in session.query(column).distinct() if row[0])
        codes.update(row[0] for row in session.query(Stock.code).all())
        doomed = [code for code in codes if classify(code)[1] in excluded]
        deleted = _delete_codes(session, doomed)
    if doomed:
        from backend.app.services.history_backfill import clear_calc_memory

        clear_calc_memory()
    return {"codes": len(doomed), "deleted": deleted}


def _delete_codes(session, codes: list[str]) -> dict[str, int]:
    from backend.app.models.tables import (
        MatchSummary,
        PatternMatch,
        Stock,
        StockTrainingRow,
    )

    deleted = {
        "daily_quotes": 0,
        "calc_bars": 0,
        "daily_features": 0,
        "limit_up_patterns": 0,
        "pattern_matches": 0,
        "match_summaries": 0,
        "stock_training_rows": 0,
        "skipped_records": 0,
        "data_quality_errors": 0,
        "stocks": 0,
    }
    for start in range(0, len(codes), 400):
        chunk = codes[start : start + 400]
        deleted["daily_quotes"] += session.query(DailyQuote).filter(DailyQuote.stock_code.in_(chunk)).delete(synchronize_session=False)
        deleted["calc_bars"] += session.query(CalcBar).filter(CalcBar.stock_code.in_(chunk)).delete(synchronize_session=False)
        deleted["daily_features"] += session.query(DailyFeature).filter(DailyFeature.stock_code.in_(chunk)).delete(synchronize_session=False)
        deleted["limit_up_patterns"] += session.query(LimitUpPattern).filter(LimitUpPattern.stock_code.in_(chunk)).delete(synchronize_session=False)
        deleted["pattern_matches"] += session.query(PatternMatch).filter(PatternMatch.target_code.in_(chunk)).delete(synchronize_session=False)
        deleted["pattern_matches"] += session.query(PatternMatch).filter(PatternMatch.sample_code.in_(chunk)).delete(synchronize_session=False)
        deleted["match_summaries"] += session.query(MatchSummary).filter(MatchSummary.target_code.in_(chunk)).delete(synchronize_session=False)
        deleted["stock_training_rows"] += session.query(StockTrainingRow).filter(StockTrainingRow.stock_code.in_(chunk)).delete(synchronize_session=False)
        deleted["skipped_records"] += session.query(SkippedRecord).filter(SkippedRecord.stock_code.in_(chunk)).delete(synchronize_session=False)
        deleted["data_quality_errors"] += session.query(DataQualityError).filter(DataQualityError.stock_code.in_(chunk)).delete(synchronize_session=False)
        deleted["stocks"] += session.query(Stock).filter(Stock.code.in_(chunk)).delete(synchronize_session=False)
    return deleted


def calc_cutoff(cutoff: date) -> date:
    """补拉线的保留线：比正式行情的保留线再往前留一个补拉回看。

    补拉线只为「保留窗口里的涨停日之前凑三根」而存在；保留线当天那只涨停票的
    前三根本来就在保留线之前。跟正式行情同一条线删，删掉的恰是它们，下一轮又要补。
    """
    from backend.app.services.history_backfill import BACKFILL_LOOKBACK_CALENDAR_DAYS

    return cutoff - timedelta(days=BACKFILL_LOOKBACK_CALENDAR_DAYS)


def purge_expired() -> dict:
    """删掉保留窗口之前的行情和标签。总结表和 summaries/ 文件不动。"""
    excluded = purge_excluded_boards()
    keep = retention_days()
    with session_scope() as session:
        days = quote_dates(session)
        if not days:
            deleted = {"daily_quotes": 0, "calc_bars": 0}
            kept = []
            cutoff = None
        elif len(days) <= keep:
            # 正式行情已经不多于保留天数。保留窗口里用不上的更早补拉 K 线仍然要删掉。
            cutoff = days[0]
            deleted = {
                "daily_quotes": 0,
                "calc_bars": session.query(CalcBar).filter(CalcBar.trade_date < calc_cutoff(cutoff)).delete(synchronize_session=False),
            }
            kept = [day.isoformat() for day in days]
        else:
            cutoff = days[-keep]
            deleted = {
                "daily_quotes": session.query(DailyQuote).filter(DailyQuote.trade_date < cutoff).delete(synchronize_session=False),
                "calc_bars": session.query(CalcBar).filter(CalcBar.trade_date < calc_cutoff(cutoff)).delete(synchronize_session=False),
                "daily_features": session.query(DailyFeature).filter(DailyFeature.trade_date < cutoff).delete(synchronize_session=False),
                "limit_up_patterns": session.query(LimitUpPattern).filter(LimitUpPattern.limit_up_date < cutoff).delete(synchronize_session=False),
                # 目标日在窗口外的一律删；引用的样本日已经被删掉的也删——
                # 留下一条指向不存在样本的明细，页面上点它就只能报「样本找不到」。
                "pattern_matches": session.query(PatternMatch).filter(
                    PatternMatch.target_date < cutoff
                ).delete(synchronize_session=False)
                + session.query(PatternMatch).filter(
                    PatternMatch.sample_limit_date < cutoff
                ).delete(synchronize_session=False),
                "match_summaries": session.query(MatchSummary).filter(MatchSummary.target_date < cutoff).delete(synchronize_session=False),
                "stock_training_rows": session.query(StockTrainingRow).filter(StockTrainingRow.label_date < cutoff).delete(synchronize_session=False),
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
        "excluded_boards": excluded,
        "last_cleanup_at": moment,
    }
