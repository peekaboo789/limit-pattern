"""脏行清理：删掉 error 级账目指到的那几根日线，删完只复查一遍。

这一层和 `quality_service` 分开是刻意的：检查只写账、一个字段都不改；删数据必须用户明说
（`confirm=True`）。原始行情是红线，派生数据才是可以随时重算的东西——所以这里删掉的
特征行、样本行、匹配行都不用备份，而删掉的那几根日线，只有质量闸门认定「这一行本身不成立」
时才动它。

复查只做一遍，**不做第二遍**。删掉中间一根线，它后面那根的 `pre_close` 就接不上了，
第二轮会冒出一批新 error；那些新 error 是上一轮删除的**结果**，不是原因。照着它们再删一轮
会顺着时间轴一路把历史啃掉——老工程真踩过这个坑，所以这里只报出来，交给下一次同步和人来判。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func

from backend.app.core.constants import QUALITY_PURGE_MAX_RATIO
from backend.app.core.logging import get_logger
from backend.app.models.tables import (
    CalcBar,
    DailyFeature,
    DailyQuote,
    DataQualityError,
    LimitUpPattern,
    PatternMatch,
    SkippedRecord,
)
from backend.app.services.quality_service import (
    ERROR,
    CHECK_LABELS,
    error_pairs,
    list_errors,
    run_checks,
)

logger = get_logger("purge")

#: 一次查询里最多带多少个代码。四千多只一次传进去，SQLite 会把语句长度用满。
_CHUNK = 400
#: 复查之后最多报出几条新 exposure。全列出来没人看得完，但它必须存在。
_EXPOSE_LIMIT = 20


def _by_day(pairs: set[tuple[str, date]]) -> dict[date, list[str]]:
    grouped: dict[date, list[str]] = {}
    for code, day in pairs:
        grouped.setdefault(day, []).append(code)
    return {day: sorted(set(codes)) for day, codes in grouped.items()}


def _chunks(items: list, size: int = _CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _count_keyed(session, model, date_column, code_column, grouped) -> int:
    total = 0
    for day, codes in sorted(grouped.items()):
        for part in _chunks(codes):
            total += (
                session.query(func.count(model.id))
                .filter(date_column == day, code_column.in_(part))
                .scalar()
                or 0
            )
    return int(total)


def _delete_keyed(session, model, date_column, code_column, grouped) -> int:
    total = 0
    for day, codes in sorted(grouped.items()):
        for part in _chunks(codes):
            total += (
                session.query(model)
                .filter(date_column == day, code_column.in_(part))
                .delete(synchronize_session=False)
            )
    return int(total)


def _as_day(value) -> date | None:
    """`window_dates` 里存的是 ISO 串；万一有别处塞进 date 对象，两种都认。"""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _pattern_ids(session, pairs: set[tuple[str, date]], end: date) -> list[int]:
    """样本行要删的两类：涨停那天自己是脏行，或者它那三天形态窗口里坐着脏行。

    后者是「窗口里同一天数了两遍」那类污染的来源——窗口的输入已经被抽掉一根，
    这一行向量算不出第二个结果，留着只会让后面的相似度读到一个再也复现不了的样本。
    """
    dirty_by_code: dict[str, set[date]] = {}
    for code, day in pairs:
        dirty_by_code.setdefault(code, set()).add(day)
    doomed: list[int] = []
    rows = (
        session.query(
            LimitUpPattern.id,
            LimitUpPattern.stock_code,
            LimitUpPattern.limit_up_date,
            LimitUpPattern.window_dates,
        )
        .filter(LimitUpPattern.limit_up_date <= end)
        .all()
    )
    for row_id, code, limit_day, window in rows:
        dirty = dirty_by_code.get(code or "", set())
        if not dirty:
            continue
        if limit_day in dirty:
            doomed.append(int(row_id))
            continue
        days = [day for day in (_as_day(item) for item in (window or [])) if day is not None]
        if any(day in dirty for day in days):
            doomed.append(int(row_id))
    return doomed


def _match_ids(session, pairs: set[tuple[str, date]]) -> list[int]:
    """匹配行两头都算：目标那天脏了，结果不成立；样本那天脏了，分数是抄来的。"""
    doomed: set[int] = set()
    grouped = _by_day(pairs)
    for day, codes in sorted(grouped.items()):
        for part in _chunks(codes):
            for row_id in (
                session.query(PatternMatch.id)
                .filter(PatternMatch.target_date == day, PatternMatch.target_code.in_(part))
                .all()
            ):
                doomed.add(int(row_id[0]))
            for row_id in (
                session.query(PatternMatch.id)
                .filter(PatternMatch.sample_limit_date == day, PatternMatch.sample_code.in_(part))
                .all()
            ):
                doomed.add(int(row_id[0]))
    return sorted(doomed)


def _delete_ids(session, model, ids: list[int]) -> int:
    total = 0
    for part in _chunks(ids):
        total += (
            session.query(model).filter(model.id.in_(part)).delete(synchronize_session=False)
        )
    return int(total)


def _error_ids_at_pairs(session, pairs: set[tuple[str, date]]) -> list[int]:
    """这条账指的是哪几个脏行，行删掉了，账就该跟着走——warning 不在这里，它记的是问题不是脏行。"""
    rows = (
        session.query(DataQualityError.id, DataQualityError.stock_code, DataQualityError.trade_date)
        .filter(DataQualityError.severity == ERROR)
        .all()
    )
    return [int(row[0]) for row in rows if (row[1] or "", row[2]) in pairs]


def purge_error_rows(
    session,
    *,
    start: date | None = None,
    end: date | None = None,
    confirm: bool = False,
    names: dict[str, str] | None = None,
) -> dict:
    """删掉 error 级日线与它们的下游。默认只算不删，`confirm=True` 才动手。"""
    lo = session.query(func.min(DailyQuote.trade_date)).scalar()
    hi = session.query(func.max(DailyQuote.trade_date)).scalar()
    begin = start or lo
    end = end or hi
    if begin is None or end is None:
        #: 空库（首启、或全部被排除板块清掉了）：没有区间可判，明说一句，不装作「查过且干净」
        return {
            "ok": True,
            "dry_run": not confirm,
            "confirm": bool(confirm),
            "range": {"start": "", "end": ""},
            "error_pairs": 0,
            "quotes_in_range": 0,
            "planned": {},
            "deleted": {},
            "ratio": 0.0,
            "refused": "",
            "recheck": None,
            "newly_exposed": [],
            "note": "库里一行行情都没有：没有可清理的区间",
        }
    if begin > end:
        begin, end = end, begin

    pairs = error_pairs(session, start=begin, end=end)
    total_rows = int(
        session.query(func.count(DailyQuote.id))
        .filter(DailyQuote.trade_date >= begin, DailyQuote.trade_date <= end)
        .scalar()
        or 0
    )
    result: dict = {
        "ok": True,
        "dry_run": not confirm,
        "confirm": bool(confirm),
        "range": {"start": begin.isoformat(), "end": end.isoformat()},
        "error_pairs": len(pairs),
        "quotes_in_range": total_rows,
        "planned": {},
        "deleted": {},
        "ratio": 0.0,
        "refused": "",
        "recheck": None,
        "newly_exposed": [],
        "note": "",
    }
    if not pairs:
        result["note"] = "error 级账目是空的：这一段区间里没有要删的脏行"
        return result

    grouped = _by_day(pairs)
    pattern_ids = _pattern_ids(session, pairs, end)
    match_ids = _match_ids(session, pairs)
    error_ids = _error_ids_at_pairs(session, pairs)
    planned = {
        "daily_quotes": _count_keyed(
            session, DailyQuote, DailyQuote.trade_date, DailyQuote.stock_code, grouped
        ),
        "calc_bars": _count_keyed(
            session, CalcBar, CalcBar.trade_date, CalcBar.stock_code, grouped
        ),
        "daily_features": _count_keyed(
            session, DailyFeature, DailyFeature.trade_date, DailyFeature.stock_code, grouped
        ),
        "skipped_records": _count_keyed(
            session, SkippedRecord, SkippedRecord.trade_date, SkippedRecord.stock_code, grouped
        ),
        "limit_up_patterns": len(pattern_ids),
        "pattern_matches": len(match_ids),
        "data_quality_errors": len(error_ids),
    }
    result["planned"] = planned
    result["ratio"] = round(planned["daily_quotes"] / total_rows, 6) if total_rows else 1.0

    #: 删掉的行数超过全天行情的固定比例，就不是「清理几条脏行」，而是检查本身跑坏了
    #: （比如某个源整段只回了半天表）。这一刀砍下去，原始行情红线就没了。
    if result["ratio"] > QUALITY_PURGE_MAX_RATIO:
        result["refused"] = (
            f"这一趟要删 {planned['daily_quotes']} 行，占区间内 {total_rows} 行的 "
            f"{result['ratio'] * 100:.2f}%，超过 {QUALITY_PURGE_MAX_RATIO * 100:.0f}% 的上限："
            f"宁可一条都不删，请先把 error 看一遍再决定"
        )
        result["ok"] = False
        logger.warning("脏行清理被比例上限挡下：%s", result["refused"])
        return result

    if not confirm:
        result["note"] = "这是预演：一个字节都没删。要真删请带 confirm=true"
        return result

    before = {
        (row.stock_code, row.trade_date, row.check_name)
        for row in session.query(DataQualityError).filter(DataQualityError.severity == ERROR).all()
    }
    deleted = {
        "daily_quotes": _delete_keyed(
            session, DailyQuote, DailyQuote.trade_date, DailyQuote.stock_code, grouped
        ),
        "calc_bars": _delete_keyed(
            session, CalcBar, CalcBar.trade_date, CalcBar.stock_code, grouped
        ),
        "daily_features": _delete_keyed(
            session, DailyFeature, DailyFeature.trade_date, DailyFeature.stock_code, grouped
        ),
        "skipped_records": _delete_keyed(
            session, SkippedRecord, SkippedRecord.trade_date, SkippedRecord.stock_code, grouped
        ),
        "limit_up_patterns": _delete_ids(session, LimitUpPattern, pattern_ids),
        "pattern_matches": _delete_ids(session, PatternMatch, match_ids),
        "data_quality_errors": _delete_ids(session, DataQualityError, error_ids),
    }
    session.flush()
    result["deleted"] = deleted
    logger.info(
        "脏行清理：删日线 %s 行、补拉 K 线 %s 行、下游 %s 行（样本 %s、匹配 %s），账目跟着删 %s 条",
        deleted["daily_quotes"],
        deleted["calc_bars"],
        deleted["daily_features"] + deleted["skipped_records"],
        deleted["limit_up_patterns"],
        deleted["pattern_matches"],
        deleted["data_quality_errors"],
    )

    #: 只复查这一遍。新冒出来的 error 报出来就停，不再删第二轮——第二轮删的是上一轮
    #: 删除动作自己造出来的缺口，照它删会把好端端的历史啃穿。
    after = run_checks(session, start=begin, end=end, names=names)
    result["recheck"] = after
    fresh = [
        row
        for row in list_errors(session, severity=ERROR, limit=500)
        if (row["stock_code"], _day_or_blank(row["trade_date"]), row["check"]) not in before
    ][:_EXPOSE_LIMIT]
    result["newly_exposed"] = fresh
    result["note"] = (
        f"删了 {deleted['daily_quotes']} 行日线，复查一遍：新暴露 {len(fresh)} 条 error"
        f"{'（只报不删，第二轮由下一次同步或人工判断）' if fresh else '，没有新暴露的 error'}"
    )
    return result


def _day_or_blank(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def purge_condition(session) -> dict:
    """给页面用的「能不能删」：有没有 error 账、指向多少只哪几天、下游会带掉多少行。"""
    pairs = error_pairs(session)
    grouped = _by_day(pairs)
    return {
        "error_pairs": len(pairs),
        "days": sorted(day.isoformat() for day in grouped),
        "codes": len({code for code, _ in pairs}),
        "checks": sorted(
            {
                CHECK_LABELS.get(row[0], row[0])
                for row in session.query(DataQualityError.check_name)
                .filter(DataQualityError.severity == ERROR)
                .all()
            }
        ),
    }


__all__ = ["purge_error_rows", "purge_condition"]
