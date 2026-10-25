"""匹配明细落库（需求第十九、二十四、二十九节）。

唯一写入路径是 `backtest_service.score_day`：首页榜、盘中榜的概率列、回测观测、
次日预测池和这张表吃的是同一个函数、同一把尺子。落库只是把那次打分的结果存下来，
**没有第二套打分**。`test_patterns_and_matches.py` 里有一条断言逐条比较
「接口现算出来的分」与「库里那一行的分」，谁另起一路当场炸。

存两样东西：

* `pattern_matches`——每只股票前 `MATCH_TOP_N` 条最像的涨停样本，带分项打分。
* `match_summaries`——它对**整个样本池**的汇总（条数、平均分、最高分、落哪一档）。

汇总单独一张表是因为从截断后的 20 条重算平均分得到的是「最像的 20 条里的平均分」，
那是另一件事，不能顶着同一个名字（老工程写全对子、再从几十万行重算，一轮实测
1,558,980 行；这一版按 `MATCH_TOP_N` 截断，一轮约「候选数 × 20」行）。

候选集**不含当日已涨停的股票**（`score_day` 剔的）：它们的形态里已经含着涨停那一天。
所以「拿一只今天涨停的股票去查匹配明细」这件事在这里天然没有行，
接口会把这句话回传（`reason = limit_up_today`），不会装作查无此人。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import case, delete, func, insert, or_, select
from sqlalchemy.orm import Session

from backend.app.algorithms.features import (
    AGGREGATE_NAMES,
    DAILY_SPEC,
    build_three_day_window,
)
from backend.app.core.constants import BAND_HIGH, MATCH_INSERT_BATCH, MATCH_TOP_N
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyQuote, MatchSummary, PatternMatch, Stock
from backend.app.providers.markets import BOARD_LABELS
from backend.app.services import pattern_service
from backend.app.services.backtest_service import score_day
from backend.app.services.feature_service import add_skip, record_skip
from backend.app.services.history_backfill import load_bars, load_bars_bulk
from backend.app.services.similarity_service import DISCLAIMER

logger = get_logger("match_service")

#: 单只股票现算时回传的「为什么没有明细」。
NO_QUOTE = "no_quote"
LIMIT_UP_TODAY = "limit_up_today"
INSUFFICIENT_WINDOW = "insufficient_window"
QUALITY_ERROR = "data_quality_error"
EMPTY_POOL = "empty_pool"
STORED = "stored"

REASONS: dict[str, str] = {
    NO_QUOTE: "这一天没有它的行情",
    LIMIT_UP_TODAY: "当天已经涨停：它的形态里含着涨停那一天，不进匹配候选",
    INSUFFICIENT_WINDOW: "库里凑不齐它的前三根K线",
    QUALITY_ERROR: "它的窗口里有 error 级数据质量异常",
    EMPTY_POOL: "样本池是空的：这一天之前没有已收盘的涨停样本",
}


def _quote_row(session: Session, code: str, day: date) -> DailyQuote | None:
    return session.scalar(
        select(DailyQuote).where(DailyQuote.stock_code == code, DailyQuote.trade_date == day)
    )


def _missing_reason(session: Session, code: str, day: date, scored: dict) -> tuple[str, str]:
    """`score_day` 没给这只股票出分时，说清是为什么。

    顺序与 `score_day` 内部的剔除顺序一致：先问有没有行情，再问当天涨停没有，
    最后才是窗口与脏行——把「今天涨停」和「没行情」混成一句「查不到」，
    用户就只能瞎试。
    """
    if scored.get("pool_size", 0) == 0:
        return EMPTY_POOL, REASONS[EMPTY_POOL]
    row = _quote_row(session, code, day)
    if row is None:
        return NO_QUOTE, REASONS[NO_QUOTE]
    if bool(row.is_limit_up):
        return LIMIT_UP_TODAY, REASONS[LIMIT_UP_TODAY]
    window = build_three_day_window(load_bars(session, code, day), day, include_as_of_date=True)
    if not window.ok:
        return INSUFFICIENT_WINDOW, REASONS[INSUFFICIENT_WINDOW]
    return QUALITY_ERROR, REASONS[QUALITY_ERROR]


def _pair_rows(target: dict, day: date) -> list[dict]:
    rows: list[dict] = []
    for match in target.get("matches") or []:
        limit_date = date.fromisoformat(str(match["limit_date"])[:10])
        rows.append(
            {
                "target_code": target["code"],
                "target_date": day,
                "sample_code": match["code"],
                "sample_limit_date": limit_date,
                "similarity_score": float(match["score"] or 0.0),
                "score_breakdown": {
                    "rank": len(rows) + 1,
                    "name": match.get("name", ""),
                    "shape": match.get("shape_score"),
                    "volume": match.get("volume_score"),
                    "volume_pct": match.get("volume_pct_score"),
                    "amount": match.get("amount_score"),
                    "band": match.get("band", ""),
                    "window": list(match.get("window") or []),
                },
            }
        )
    return rows


def _summary_row(item: dict, day: date, scored: dict) -> dict:
    return {
        "target_code": item["code"],
        "target_date": day,
        "sample_count": int(item.get("sample_count") or 0),
        "high_similarity_count": int(item.get("high_similarity_count") or 0),
        "avg_similarity": item.get("avg_similarity"),
        "max_similarity": item.get("max_similarity"),
        "band": str(item.get("band") or ""),
        "pool_dates": list(scored.get("pool_dates") or []),
        "pool_size": int(scored.get("pool_size") or 0),
    }


def _drop_targets(session: Session, day: date, codes: list[str]) -> None:
    """先删这些 (代码, 日) 的旧行再写：重跑一次只留一份答案，不留旧口径的残行。"""
    if not codes:
        return
    session.execute(
        delete(PatternMatch).where(
            PatternMatch.target_date == day, PatternMatch.target_code.in_(codes)
        )
    )
    session.execute(
        delete(MatchSummary).where(
            MatchSummary.target_date == day, MatchSummary.target_code.in_(codes)
        )
    )


def store_scored(session: Session, day: date, scored: dict) -> dict:
    """把一次 `score_day` 的结果写进库。批次按 `MATCH_INSERT_BATCH` 切。"""
    items = list(scored.get("items") or [])
    codes = [str(item["code"]) for item in items]
    _drop_targets(session, day, codes)
    pairs: list[dict] = []
    summaries = []
    for item in items:
        pairs.extend(_pair_rows(item, day))
        summaries.append(_summary_row(item, day, scored))
    written = 0
    for start in range(0, len(pairs), MATCH_INSERT_BATCH):
        chunk = pairs[start : start + MATCH_INSERT_BATCH]
        session.execute(insert(PatternMatch), chunk)
        written += len(chunk)
    for start in range(0, len(summaries), MATCH_INSERT_BATCH):
        session.execute(insert(MatchSummary), summaries[start : start + MATCH_INSERT_BATCH])
    session.flush()
    return {"match_count": written, "summary_count": len(summaries), "target_count": len(items)}


def match_target(session: Session, code: str, day: date) -> dict:
    """现算一只股票的匹配明细并落库。页面点「重算这一只」走的就是这里。

    与全市场扫描共用 `score_day`：同一只股票在同一天不可能有两个分。
    """
    scored = score_day(session, day, top_matches=MATCH_TOP_N, only_codes={code})
    item = next((row for row in scored.get("items") or [] if row["code"] == code), None)
    if item is None:
        reason, detail = _missing_reason(session, code, day, scored)
        names = pattern_service.stock_names(session)
        #: 失败回执的键与 GET 那条（`stored_matches`）逐项对齐：页面只需要按 `ok` 分支，
        #: 不用为「读库没读到」和「现算没算出」写两套兜底；`items`/`summary` 缺键会让
        #: 同一个 `ok:false` 在两个按钮后面长出两种形状。
        return {
            "ok": False,
            "reason": reason,
            "detail": detail,
            "disclaimer": DISCLAIMER,
            "trade_date": day.isoformat(),
            "stock_code": code,
            "stock_name": names.get(code, ""),
            "pool_dates": list(scored.get("pool_dates") or []),
            "pool_size": int(scored.get("pool_size") or 0),
            "summary": None,
            "items": [],
            "skipped_samples": list(scored.get("skipped_samples") or []),
        }
    written = store_scored(session, day, {**scored, "items": [item]})
    payload = stored_matches(session, code, day)
    payload["written"] = written
    payload["skipped_samples"] = list(scored.get("skipped_samples") or [])
    return payload


def scan_and_store(session: Session, day: date, *, stats: dict) -> dict:
    """全市场匹配扫描并落库。这是数据管理页那个按钮背后的活。"""
    scored = score_day(session, day, top_matches=MATCH_TOP_N)
    written = store_scored(session, day, scored)
    stats["pool_size"] = int(scored.get("pool_size") or 0)
    stats["universe_count"] = int(scored.get("universe_count") or 0)
    stats["limit_up_count"] = int(scored.get("limit_up_count") or 0)
    stats["match_count"] = written["match_count"]
    stats["summary_count"] = written["summary_count"]
    stats["target_count"] = written["target_count"]
    if scored.get("missing_bars"):
        add_skip(stats, INSUFFICIENT_WINDOW, int(scored["missing_bars"]))
        for code in _codes_without_window(session, day):
            record_skip(
                session,
                day=day,
                stock_code=code,
                stage="match",
                reason=INSUFFICIENT_WINDOW,
                detail="凑不齐评估日前四根K线（含当天）",
            )
    if scored.get("dirty_skipped"):
        add_skip(stats, QUALITY_ERROR, int(scored["dirty_skipped"]))
    if not scored.get("pool_size"):
        add_skip(stats, EMPTY_POOL, 1)
    return stats


def _codes_without_window(session: Session, day: date) -> list[str]:
    """当天有行情但构不出窗口的代码——只为把跳过账记到具体股票上。

    一次 `load_bars_bulk` 取全市场再在内存里判：逐只 `load_bars` 是四千多趟查询，
    为了记一笔账把扫描拖成十分钟不值。
    """
    rows = session.execute(
        select(DailyQuote.stock_code).where(DailyQuote.trade_date == day).distinct()
    ).scalars().all()
    bars_by_code = load_bars_bulk(session, day)
    return sorted(
        code
        for code in rows
        if not build_three_day_window(
            bars_by_code.get(code) or [], day, include_as_of_date=True
        ).ok
    )


def stored_matches(session: Session, code: str, day: date) -> dict:
    """库里已有的明细。没有汇总行就返回 `ok: false` 并说清为什么，不回一张空表。"""
    summary = session.scalar(
        select(MatchSummary).where(
            MatchSummary.target_code == code, MatchSummary.target_date == day
        )
    )
    rows = list(
        session.scalars(
            select(PatternMatch)
            .where(PatternMatch.target_code == code, PatternMatch.target_date == day)
            .order_by(PatternMatch.similarity_score.desc(), PatternMatch.sample_code)
        ).all()
    )
    names = pattern_service.stock_names(session)
    if summary is None and not rows:
        row = _quote_row(session, code, day)
        reason = NO_QUOTE if row is None else (
            LIMIT_UP_TODAY if bool(row.is_limit_up) else "not_scanned"
        )
        detail = REASONS.get(reason, "这一天还没有跑过匹配扫描")
        return {
            "ok": False,
            "reason": reason,
            "detail": detail,
            "disclaimer": DISCLAIMER,
            "trade_date": day.isoformat(),
            "stock_code": code,
            "stock_name": names.get(code, ""),
            "pool_dates": [],
            "pool_size": 0,
            "summary": None,
            "items": [],
        }
    items = [
        {
            "rank": int((row.score_breakdown or {}).get("rank") or index),
            "code": row.sample_code,
            "name": (row.score_breakdown or {}).get("name") or names.get(row.sample_code, ""),
            "limit_date": row.sample_limit_date.isoformat(),
            "score": round(float(row.similarity_score), 2),
            "band": (row.score_breakdown or {}).get("band", ""),
            "shape_score": (row.score_breakdown or {}).get("shape"),
            "volume_score": (row.score_breakdown or {}).get("volume"),
            "volume_pct_score": (row.score_breakdown or {}).get("volume_pct"),
            "amount_score": (row.score_breakdown or {}).get("amount"),
            "window": list((row.score_breakdown or {}).get("window") or []),
        }
        for index, row in enumerate(rows, start=1)
    ]
    return {
        "ok": True,
        "reason": STORED,
        "detail": "",
        "disclaimer": DISCLAIMER,
        "trade_date": day.isoformat(),
        "stock_code": code,
        "stock_name": names.get(code, ""),
        "pool_dates": list(summary.pool_dates or []) if summary else [],
        "pool_size": int(summary.pool_size) if summary else len(items),
        "summary": None if summary is None else _summary_out(summary),
        "items": items,
    }


def _summary_out(row: MatchSummary) -> dict:
    return {
        "sample_count": int(row.sample_count or 0),
        "high_similarity_count": int(row.high_similarity_count or 0),
        "high_similarity_rate": (
            round(int(row.high_similarity_count or 0) / int(row.sample_count) * 100.0, 2)
            if int(row.sample_count or 0) > 0
            else None
        ),
        "max_similarity": None if row.max_similarity is None else round(float(row.max_similarity), 2),
        "avg_similarity": None if row.avg_similarity is None else round(float(row.avg_similarity), 2),
        "band": row.band or "",
        "pool_dates": list(row.pool_dates or []),
        "pool_size": int(row.pool_size or 0),
        "created_at": row.created_at.isoformat(sep=" ", timespec="seconds") if row.created_at else "",
    }


def matches_by_code(session: Session, day: date, codes: list[str]) -> dict[str, list[dict]]:
    """AI 证据的连板率吃这个：每只股票库里的 Top 匹配（代码、涨停日、分数）。

    读库而不是现算，是因为「这条证据追不追得到出处」要有确定答案：
    盘后再敲一次现算，样本池可能已经多了一天，模型引用的就成了当天看不到的数。
    """
    if not codes:
        return {}
    rows = list(
        session.scalars(
            select(PatternMatch)
            .where(PatternMatch.target_date == day, PatternMatch.target_code.in_(codes))
            .order_by(PatternMatch.target_code, PatternMatch.similarity_score.desc())
        ).all()
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.target_code, []).append(
            {
                "code": row.sample_code,
                "limit_date": row.sample_limit_date.isoformat(),
                "score": round(float(row.similarity_score), 2),
            }
        )
    for code in grouped:
        grouped[code] = grouped[code][:MATCH_TOP_N]
    return grouped


#: 列表页排序认的键，唯一一份。页面上那个下拉读回包里的 `sort_options`，
#: 不是在前端再抄一遍字段名——抄了就会有「页面能选、接口回 422」那一天。
SUMMARY_SORTS = {
    "max": "最高相似度",
    "avg": "全池平均分",
    "rate": "过高分线比例",
    "count": "过高分线条数",
    "samples": "参与比对的样本条数",
    "code": "代码",
    "name": "名称",
}


def _rate_expression():
    """`_summary_out` 那个 `high_similarity_rate` 的同一把尺子，在库里除出来。

    分母（实际参与比对的样本条数）为 0 时回 NULL，不是 0%——「一条样本都没比上」
    和「比上了但一条没过线」是两个不同的事实，排序时前者自然垫到空值那一档。
    """
    return case(
        (
            MatchSummary.sample_count > 0,
            MatchSummary.high_similarity_count * 100.0 / MatchSummary.sample_count,
        ),
        else_=None,
    )


def _sort_expression(sort: str):
    if sort == "rate":
        return _rate_expression()
    if sort == "count":
        return MatchSummary.high_similarity_count
    if sort == "samples":
        return MatchSummary.sample_count
    if sort == "avg":
        return MatchSummary.avg_similarity
    if sort == "code":
        return MatchSummary.target_code
    if sort == "name":
        return Stock.name
    return MatchSummary.max_similarity


def summary_page(
    session: Session,
    day: date,
    *,
    limit: int = 100,
    offset: int = 0,
    q: str = "",
    board: str = "",
    sort: str = "max",
    order: str = "desc",
) -> dict:
    """某一天所有落库汇总的分页，可按代码/名称与板块筛、按任一相似度口径排。

    筛选、排序、分页全在 SQL 里做：这一页只取回 `limit` 行，页面拿到的 `total` 是
    **筛完之后**的条数。数据管理页仍然用默认那档（`sort=max&order=desc`）数「今天扫了多少只」，
    同一张表、同一个排序，不会因为多了一个列表页就长出第二套读法。

    排序键允许是 NULL 的（`avg_similarity` / `max_similarity` 在池子为空时是 None）：
    SQLite 把 NULL 当最小，所以倒序时空值自然垫后、升序时排在最前，页面按「—」显示。
    次关键字恒为代码升序，否则同分的行在翻页时会重复出现或整页漏掉。
    """
    if sort not in SUMMARY_SORTS:
        raise ValueError(f"不支持的排序字段：{sort}")
    if order not in ("desc", "asc"):
        raise ValueError(f"不支持的排序方向：{order}")

    keyword = (q or "").strip()
    board_name = (board or "").strip()
    conditions = [MatchSummary.target_date == day]
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(MatchSummary.target_code.like(pattern), Stock.name.like(pattern)))
    base_conditions = list(conditions)
    if board_name:
        conditions.append(Stock.board == board_name)

    total = int(
        session.scalar(
            select(func.count())
            .select_from(MatchSummary)
            .outerjoin(Stock, Stock.code == MatchSummary.target_code)
            .where(*conditions)
        )
        or 0
    )
    # 板块下拉的条数跟着关键词走、但不跟着板块自己走：选了「创业板」之后
    # 其他板块不能从下拉里消失，否则想换回去就只能先清空选择。
    boards = [
        {
            "board": value or "",
            "label": BOARD_LABELS.get(value or "", "没登记板块"),
            "count": int(count),
        }
        for value, count in session.execute(
            select(Stock.board, func.count())
            .select_from(MatchSummary)
            .outerjoin(Stock, Stock.code == MatchSummary.target_code)
            .where(*base_conditions)
            .group_by(Stock.board)
            .order_by(func.count().desc(), Stock.board)
        ).all()
    ]
    direction = _sort_expression(sort)
    direction = direction.desc() if order == "desc" else direction.asc()
    rows = list(
        session.execute(
            select(MatchSummary, Stock.name, Stock.board)
            .outerjoin(Stock, Stock.code == MatchSummary.target_code)
            .where(*conditions)
            .order_by(direction, MatchSummary.target_code.asc())
            .limit(int(limit))
            .offset(int(offset))
        ).all()
    )
    return {
        "trade_date": day.isoformat(),
        "total": total,
        "limit": int(limit),
        "offset": int(offset),
        "q": keyword,
        "board": board_name,
        "sort": sort,
        "order": order,
        "sort_options": [{"key": key, "label": label} for key, label in SUMMARY_SORTS.items()],
        "boards": boards,
        "items": [
            {
                "code": row.target_code,
                "name": name or "",
                "board": board_value or "",
                #: 板块中文名由这里出：前端自己再写一份 `main_board → 主板` 的映射，
                #: 就是第二把尺子，后端哪天加一档（北交所）页面会跟着漏。
                "board_label": BOARD_LABELS.get(board_value or "", "没登记板块"),
                **_summary_out(row),
            }
            for row, name, board_value in rows
        ],
    }


#: 导出表的列名。顺序与 `_pair_rows` 写进 `score_breakdown` 的那几项一一对应，
#: 前端与 Excel 读的是同一份落库数据，不是第三次计算。
CSV_COLUMNS = (
    "目标代码",
    "目标名称",
    "目标日期",
    "样本代码",
    "样本名称",
    "样本涨停日",
    "排名",
    "相似度",
    "档位",
    "形态分",
    "量能分",
    "量比分",
    "成交额分",
    "样本对比池日期",
)


def export_csv(
    session: Session, day: date, *, top_targets: int = 200, code: str | None = None
) -> tuple[str, int, bool]:
    """某一天落库匹配明细的 CSV。返回文本、行数、有没有被截断。

    导出吃 `match_summaries` 的排序（按最高相似度）取前 `top_targets` 只，
    而不是把当天几万行全吐出去：几十万行的 CSV 只会让 Excel 打不开，
    而「今天最像的前 200 只」才是有人要看的那一份。给了 `code` 就只导这一只。
    截断必须说出来（第三个返回值），页面据此加一句「只导出了前 N 只」——
    默默少给数据比不给更难查。
    """
    names = pattern_service.stock_names(session)
    summaries = session.execute(
        select(MatchSummary.target_code, MatchSummary.max_similarity, MatchSummary.pool_dates)
        .where(MatchSummary.target_date == day)
        .order_by(MatchSummary.max_similarity.desc(), MatchSummary.target_code)
    ).all()
    if code:
        summaries = [row for row in summaries if row[0] == code]
    truncated = len(summaries) > int(top_targets)
    picked = summaries[: int(top_targets)]
    targets = [str(row[0]) for row in picked]
    pool_by_target = {
        str(row[0]): ",".join(str(item) for item in (row[2] or [])) for row in picked
    }
    if not targets:
        return _csv_text([]), 0, False
    pairs = list(
        session.scalars(
            select(PatternMatch).where(
                PatternMatch.target_date == day, PatternMatch.target_code.in_(targets)
            )
        ).all()
    )
    order = {item: index for index, item in enumerate(targets)}
    rows_out: list[list] = []
    for row in pairs:
        breakdown = dict(row.score_breakdown or {})
        rows_out.append(
            [
                row.target_code,
                names.get(row.target_code, ""),
                row.target_date.isoformat(),
                row.sample_code,
                str(breakdown.get("name") or names.get(row.sample_code, "")),
                row.sample_limit_date.isoformat(),
                breakdown.get("rank"),
                round(float(row.similarity_score), 2),
                breakdown.get("band", ""),
                breakdown.get("shape"),
                breakdown.get("volume"),
                breakdown.get("volume_pct"),
                breakdown.get("amount"),
                pool_by_target.get(row.target_code, ""),
            ]
        )
    rows_out.sort(
        key=lambda item: (
            order.get(item[0], len(order)),
            int(item[6] or 0),
        )
    )
    return _csv_text(rows_out), len(rows_out), truncated


def _csv_text(rows: list[list]) -> str:
    """纯 CSV 文本，不含 BOM——BOM 由接口层用 `utf-8-sig` 编码时加，与 `/export` 同一口径。

    这里不加是为了不让「谁负责 BOM」出现两份答案：服务只产文本，下载格式归接口。
    """
    lines = [",".join(CSV_COLUMNS)]
    lines.extend(",".join(_csv_cell(item) for item in row) for row in rows)
    return "\r\n".join(lines) + "\r\n"


def _csv_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
    if any(mark in text for mark in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def high_similarity_share(session: Session, day: date) -> dict:
    """当天落库汇总里高档占比——评分模型的 `pattern_match_rate` 吃这个数。

    没跑过扫描时给 None：把「没有明细」当成「0% 高相似」会把风险分压低一档。
    """
    rows = session.execute(
        select(MatchSummary.max_similarity, MatchSummary.high_similarity_count, MatchSummary.sample_count).where(
            MatchSummary.target_date == day
        )
    ).all()
    if not rows:
        return {"targets": 0, "high_rate_percent": None, "source": "no_scan"}
    highs = sum(int(count or 0) for _max, count, total in rows)
    totals = sum(int(total or 0) for _max, _count, total in rows)
    return {
        "targets": len(rows),
        "high_rate_percent": None if totals <= 0 else round(highs / totals * 100.0, 2),
        "high_threshold": BAND_HIGH,
        "source": "match_summaries",
    }


def compare_with_sample(
    session: Session, code: str, day: date, sample_code: str, sample_limit_date: date
) -> dict:
    """左右对比：目标当天的三日窗口 vs 那条涨停样本落库的三日窗口，逐字段差值。

    两边的形状都来自 `pattern_service.day_detail`，字段名取自 `DAILY_SPEC`——
    页面上不写第二份清单。样本按 (代码, 涨停日) 定位：同一只股票一周里可以涨停三次，
    只给代码会拿到不知道哪一天的窗口。
    """
    names = pattern_service.stock_names(session)
    sample = pattern_service.pattern_row(session, sample_code, sample_limit_date)
    if sample is None:
        raise LookupError(
            f"{sample_code} 在 {sample_limit_date.isoformat()} 没有落库的涨停样本"
        )
    window = build_three_day_window(load_bars(session, code, day), day, include_as_of_date=True)
    if not window.ok:
        raise ValueError(f"{code} 凑不齐完整三日形态：{window.reason or '行情不足'}")
    target_days = [pattern_service.day_detail(item) for item in window.day_features]
    sample_days = list(sample.day_details or [])
    return {
        "target": {
            "code": code,
            "name": names.get(code, ""),
            "as_of_date": day.isoformat(),
            "days": target_days,
            "aggregates": _aggregates_from_vector(window.vector),
        },
        "sample": {
            "code": sample_code,
            "name": names.get(sample_code, ""),
            "as_of_date": sample_limit_date.isoformat(),
            "days": sample_days,
            "aggregates": pattern_service.aggregates_of(sample),
        },
        "differences": _differences(target_days, sample_days),
        "score": _stored_score(session, code, day, sample_code, sample_limit_date),
        "disclaimer": DISCLAIMER,
    }


def _stored_score(
    session: Session, code: str, day: date, sample_code: str, sample_limit_date: date
) -> float | None:
    """这一对落在库里的那个分。没落库时给 None——不现算第二个数冒充同一个口径。"""
    row = session.scalar(
        select(PatternMatch)
        .where(
            PatternMatch.target_code == code,
            PatternMatch.target_date == day,
            PatternMatch.sample_code == sample_code,
            PatternMatch.sample_limit_date == sample_limit_date,
        )
        .limit(1)
    )
    return None if row is None else round(float(row.similarity_score), 2)


def _aggregates_from_vector(vector: list[float]) -> dict[str, float]:
    return dict(zip(AGGREGATE_NAMES, vector[len(DAILY_SPEC) * 3 :]))


def _differences(target_days: list[dict], sample_days: list[dict]) -> list[dict]:
    """按「日 → 字段」列差值。任何一边缺值，差值就是 None，不是 0。"""
    rows: list[dict] = []
    for label, index in (("T-3", 0), ("T-2", 1), ("T-1", 2)):
        target_day = target_days[index] if index < len(target_days) else {}
        sample_day = sample_days[index] if index < len(sample_days) else {}
        target_features = dict(target_day.get("features") or {})
        sample_features = dict(sample_day.get("features") or {})
        for name, group in DAILY_SPEC:
            rows.append(
                {
                    "day": label,
                    "field": name,
                    "group": group,
                    "target": target_features.get(name),
                    "sample": sample_features.get(name),
                    "difference": _difference(target_features.get(name), sample_features.get(name)),
                }
            )
    return rows


def _difference(left, right) -> float | None:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    return round(float(left) - float(right), 6)


__all__ = [
    "CSV_COLUMNS",
    "EMPTY_POOL",
    "INSUFFICIENT_WINDOW",
    "LIMIT_UP_TODAY",
    "NO_QUOTE",
    "QUALITY_ERROR",
    "REASONS",
    "STORED",
    "compare_with_sample",
    "export_csv",
    "high_similarity_share",
    "match_target",
    "matches_by_code",
    "scan_and_store",
    "score_day",
    "store_scored",
    "stored_matches",
    "summary_page",
]
