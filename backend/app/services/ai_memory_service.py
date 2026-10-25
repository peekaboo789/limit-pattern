"""收盘后落两份 JSON：`model_memory/`（形态 → 次日实际结果）与 `failure_logs/`（AI 的误判）。

这是「让 AI 学到规律」那条链路的第二层：SQLite 里存的是市场特征与 AI 建议，
这一层把它们渲染成**一天一个文件**的配对样本，交给模型读。两条硬取向：

1. **只渲染，不造数据。** 每个数字都从库里现读（`daily_quotes` / `daily_features` /
   `ai_pick_results` / `stocks`），本模块**不写任何表**。库里没有的那一格，JSON 里就是
   `null`，并且在 `meta.field_gaps` 里写明为什么没有——绝不悄悄补 0，也绝不悄悄删掉这一格。
2. **不删别的日子。** 与 `intraday_capture` 那种「只留当天」的落盘相反：那一份是窗口冻结值，
   留着旧的会算出错榜；这两份是长期样本库，要的就是「2026-10-01.json、2026-10-02.json……」
   按天攒下去。所以这里没有任何删除路径。

为什么导出**两个**日子（当天 + 库里上一个交易日）是这条链路的关键：
当天这一份里的 `next_day` 必然是 null——第二天还没开盘。等明天的行情入库后再重写昨天那一天，
`next_day` 才会被填上，那个样本才从「只有形态」变成「形态 + 结果」。
只导当天、不回填前一天，样本库就永远停在一半。

一分钱不花：对账读的是已落的 `ai_pick_results` 与 `daily_quotes`，渲染只是把这两处摊成文件，
一次网关都不敲，所以随时可以重跑，重跑也不多花钱。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.clock import SHANGHAI
from backend.app.core.constants import MEMORY_STREAK_LOOKBACK_DAYS
from backend.app.core.logging import get_logger
from backend.app.models.tables import AiPickResult, DailyFeature, DailyQuote, Stock
from backend.app.services.ai_picks import (
    VERDICT_HIT,
    VERDICT_MISS,
    VERDICT_PENDING,
    hit_rate,
)

logger = get_logger("ai_memory_service")

#: 两份目录的名字来自需求原话，不另起名
MODEL_MEMORY_DIR_NAME = "model_memory"
FAILURE_LOG_DIR_NAME = "failure_logs"

DISCLAIMER = (
    "这一份文件由本机库里的真实行情渲染，不含任何合成样本；"
    "它是给模型读的历史配对样本，不是涨停概率、更不是收益预测，只供个人研究与技术验证。"
)

#: 这三列本库**从来没有**：写清楚，否则读 JSON 的模型会以为「换手率是 null」等于「那天没人换手」。
STATIC_FIELD_GAPS: tuple[str, ...] = (
    "涨停时间 / 封板次数 / 打开次数：本库没有落过这三列（`daily_quotes` 的列里没有它们），"
    "所以本文件里根本没有这三项的键，而不是「值为 0」。",
    "题材板块（如「人工智能」）本库没有：`daily_quotes.limit_up_reason` 存的是 "
    "matched_limit_price 这类**涨停判定标记**（记的是「这行凭什么被判涨停」），"
    "`stocks.board` 是 main_board/chinext/star 这种交易所板块，两者都不是题材。",
    "连板数 `streak_days` 是按库里真有行情的交易日往前现数出来的连续涨停天数；"
    "遇到没有行情的日子就停止累加，并把 `streak_truncated` 标为 true —— "
    "把它当成「断了」会少报，当成「没断」会多报，两者都是在替库里没有的事下结论。",
    "本库的明细只保留最近若干个交易日（设置页那格，默认 5 天）：更早的日子在这两份文件里"
    "会随保留窗口一起变薄。长期样本要靠每天落盘的这两份文件本身攒，不能靠库。",
)

#: 本库有列但公开源整列缺的那些。带当天实测条数，写在这里而不是页面上：
#: JSON 会被拿去喂别的模型，那些模型读不到本项目的 README。
_NULL_TRACKED_COLUMNS = ("amount", "turnover_rate")


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def memory_dir(root: Path | None = None) -> Path:
    from backend.app.core.config import data_dir

    return (Path(root) if root else data_dir()) / MODEL_MEMORY_DIR_NAME


def failure_dir(root: Path | None = None) -> Path:
    from backend.app.core.config import data_dir

    return (Path(root) if root else data_dir()) / FAILURE_LOG_DIR_NAME


def memory_file(trade_date: date, root: Path | None = None) -> Path:
    return memory_dir(root) / f"{trade_date.isoformat()}.json"


def failure_file(trade_date: date, root: Path | None = None) -> Path:
    return failure_dir(root) / f"{trade_date.isoformat()}.json"


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


def _number(value: Any) -> float | None:
    """库里那一格折成浮点数。缺的、坏的都是 None——「不知道」不能写成一个具体的数。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _percent_over_pre_close(value: Any, pre_close: Any) -> float | None:
    """相对前收的百分数（开盘、最高都用这一个口径）。缺一边的数就是 None。"""
    number = _number(value)
    base = _number(pre_close)
    if number is None or not base:
        return None
    return round((number / base - 1.0) * 100.0, 4)


def _quotes_on(session: Session, trade_date: date) -> int:
    """库里这一天的行情行数。0 就是「这一天还没入库」，与「这一天没人涨停」是两件事。"""
    return int(
        session.scalar(select(func.count(DailyQuote.id)).where(DailyQuote.trade_date == trade_date))
        or 0
    )


def _null_counts(session: Session, trade_date: date) -> list[str]:
    """把这一天整列为空的列**实测**出来写进 `field_gaps`。

    为什么现算而不写死一句话：公开源哪几列出数是在变的（换源、某天补上了换手率），
    一句写死的「换手率整列为空」会在某一天变成假话。这里数的是「这一天 N 行里有几行是空的」。
    """
    gaps: list[str] = []
    total = _quotes_on(session, trade_date)
    if not total:
        return gaps
    for column in _NULL_TRACKED_COLUMNS:
        field = getattr(DailyQuote, column)
        nulls = int(
            session.scalar(
                select(func.count(DailyQuote.id)).where(
                    DailyQuote.trade_date == trade_date, field.is_(None)
                )
            )
            or 0
        )
        if nulls == total:
            gaps.append(
                f"{column}：{trade_date.isoformat()} 这 {total} 行里 {nulls} 行都是 null —— "
                f"这一列当天整库没出数。null 是「本机没有这一列的数」，不是「等于 0」。"
            )
        elif nulls:
            gaps.append(
                f"{column}：{trade_date.isoformat()} 这 {total} 行里有 {nulls} 行是 null（缺数不是 0）。"
            )
    return gaps


def _meta(session: Session, trade_date: date, *, kind: str) -> dict[str, Any]:
    return {
        "date": trade_date.isoformat(),
        "kind": kind,
        "generated_at": _now(),
        "generated_by": "limit-pattern / 每日 JSON 快照（只渲染，不改库）",
        #: 判据日在库里有没有行情行；0 就是「还没入库」
        "quotes_in_library": _quotes_on(session, trade_date),
        "field_gaps": [*_null_counts(session, trade_date), *STATIC_FIELD_GAPS],
        "disclaimer": DISCLAIMER,
    }


def _next_trade_date(session: Session, trade_date: date) -> date | None:
    """库里比这一天更晚的**最早**一个有行情的交易日；没有就是「第二天结果还没出来」。"""
    return session.scalar(
        select(func.min(DailyQuote.trade_date)).where(DailyQuote.trade_date > trade_date)
    )


def _previous_trade_dates(session: Session, trade_date: date) -> list[date]:
    """库里不晚于这一天的交易日，倒序（含当天）。数连板与取 T-1/T-2/T-3 都要它。"""
    rows = session.scalars(
        select(DailyQuote.trade_date)
        .where(DailyQuote.trade_date <= trade_date)
        .group_by(DailyQuote.trade_date)
        .order_by(DailyQuote.trade_date.desc())
        .limit(MEMORY_STREAK_LOOKBACK_DAYS)
    ).all()
    return [row for row in rows if isinstance(row, date)]


def previous_trade_date(session: Session, trade_date: date) -> date | None:
    """库里**严格早于**这一天、且真有行情行的最近一个交易日；一个都没有就返回 None。

    每日流水线要用它回填前一份快照：那一天的 `next_day` 只有等今天的行情入库才写得出来。
    判据用「库里有行情行」而不是交易日历，因为回填的意义就是补上真实结果——
    日历上有这一天而库里没有数据时，重写完仍然是半个样本，白花一次磁盘。
    """
    earlier = [day for day in _previous_trade_dates(session, trade_date) if day < trade_date]
    return earlier[0] if earlier else None


def _bar(quote: DailyQuote | None, names: dict[str, str]) -> dict[str, Any] | None:
    """一根日线。没有行情行就返回 None（不是补零）。"""
    if quote is None:
        return None
    return {
        "trade_date": _iso(quote.trade_date),
        "name": names.get(quote.stock_code, ""),
        "open": _number(quote.open),
        "high": _number(quote.high),
        "low": _number(quote.low),
        "close": _number(quote.close),
        "pre_close": _number(quote.pre_close),
        "change_percent": _number(quote.change_percent),
        "volume": _number(quote.volume),
        "amount": _number(quote.amount),
        "amount_estimated": bool(quote.amount_estimated),
        "turnover_rate": _number(quote.turnover_rate),
        "is_limit_up": bool(quote.is_limit_up),
        "is_limit_down": bool(quote.is_limit_down),
        "limit_up_price": _number(quote.limit_up_price),
        "limit_up_percent": _number(quote.limit_up_percent),
        "source": quote.source or "",
    }


def _quotes_for_codes(
    session: Session, codes: list[str], days: list[date]
) -> dict[tuple[str, date], DailyQuote]:
    """把 (代码, 日期) → 行情行一次取齐：连板与前三日都要用它，逐只查会打十几趟查询。"""
    if not codes or not days:
        return {}
    rows = session.scalars(
        select(DailyQuote).where(DailyQuote.stock_code.in_(codes), DailyQuote.trade_date.in_(days))
    ).all()
    return {(str(row.stock_code), row.trade_date): row for row in rows}


def _names(session: Session, codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    rows = session.query(Stock.code, Stock.name).filter(Stock.code.in_(codes)).all()
    return {str(code): str(name or "") for code, name in rows}


def _features_of(session: Session, code: str, trade_date: date) -> dict[str, Any]:
    """`daily_features` 里这一天的形态指标原值。没有行就回空 dict——不补零、也不编。"""
    row = session.scalars(
        select(DailyFeature)
        .where(DailyFeature.stock_code == code, DailyFeature.trade_date == trade_date)
        .limit(1)
    ).first()
    if row is None:
        return {}
    raw = row.features if isinstance(row.features, dict) else {}
    return {str(key): _number(value) for key, value in raw.items()}


def _streak(
    index: dict[tuple[str, date], DailyQuote], code: str, days: list[date]
) -> tuple[int, bool]:
    """从判据日往前数连续涨停天数。返回（连板数, 这个数是不是只能当**下界**读）。

    两种停止方式必须分开：

    * 遇到一根不是涨停的日线 → 连板确实断了，第二个返回值 False，这个数是确值。
    * 遇到**没有行情行**的一天（停牌、上市前、那天同步缺数）→ 断没断不知道，只能停在这里
      并返回 True：当成「断了」会少报，当成「没断」会多报。
    * 数满了回溯窗口还没断，同样返回 True——那是窗口截断，不是结论。
    """
    streak = 0
    for day in days:
        quote = index.get((code, day))
        if quote is None:
            return streak, True
        if not quote.is_limit_up:
            return streak, False
        streak += 1
    return streak, True


def build_sample_document(session: Session, trade_date: date) -> dict[str, Any]:
    """第二层的主体：这一天每只涨停股票的「形态特征 → 第二天实际结果」配对样本。

    选涨停股而不是全市场，是这份样本的语义决定的：`features` 与 `streak_days` 只对
    当天涨停的股票有意义，`next_day` 问的是「第二天有没有续上」。全市场那份配对本来
    就在 `stock_training_data` 里（每行 = 一只股票一个判据日 + 前三日行情 + 标签），
    要按天导全量随时能在这一层换个过滤器做，但那是每天几 MB 的文件，不是「喂给模型读」的形状。
    """
    limit_ups = list(
        session.scalars(
            select(DailyQuote)
            .where(DailyQuote.trade_date == trade_date, DailyQuote.is_limit_up == True)  # noqa: E712
            .order_by(DailyQuote.stock_code.asc())
        ).all()
    )
    codes = [str(row.stock_code) for row in limit_ups]
    names = _names(session, codes)
    days = _previous_trade_dates(session, trade_date)
    index = _quotes_for_codes(session, codes, days)
    next_day = _next_trade_date(session, trade_date)
    next_quotes = _quotes_for_codes(session, codes, [next_day] if next_day else [])

    samples: list[dict[str, Any]] = []
    for row in limit_ups:
        code = str(row.stock_code)
        streak, truncated = _streak(index, code, days)
        prior: dict[str, Any] = {}
        for lag in (1, 2, 3):
            #: days[0] 是判据日自己，T-1 往后就是 days[lag]
            day = days[lag] if len(days) > lag else None
            prior[f"T-{lag}"] = _bar(index.get((code, day)) if day else None, names)
        samples.append(
            {
                "code": code,
                "name": names.get(code, ""),
                "day": {
                    **(_bar(row, names) or {}),
                    "features": _features_of(session, code, trade_date),
                    "streak_days": streak,
                    "streak_truncated": truncated,
                    #: 这一行凭什么被判涨停的标记（matched_limit_price 这类），不是题材
                    "limit_up_flag_reason": row.limit_up_reason or "",
                },
                "prev_days": prior,
                #: 第二天结果还没入库时这一格是 null，并在 next_day_status 里说明为什么
                "next_day": None,
                "next_day_status": "",
            }
        )
        if next_day is None:
            samples[-1]["next_day_status"] = (
                f"库里 {trade_date.isoformat()} 之后还没有任何行情日，第二天的结果还不存在"
            )
        else:
            quote = next_quotes.get((code, next_day))
            if quote is None:
                samples[-1]["next_day_status"] = (
                    f"{_iso(next_day)} 这只股票没有行情行（停牌或当日缺数），第二天结果未知"
                )
            else:
                samples[-1]["next_day"] = {
                    "trade_date": _iso(next_day),
                    "is_limit_up": bool(quote.is_limit_up),
                    #: 开盘涨幅 =（今开 − 前收）/ 前收 × 100；缺一个就是 null
                    "open_change_percent": _percent_over_pre_close(quote.open, quote.pre_close),
                    "max_change_percent": _percent_over_pre_close(quote.high, quote.pre_close),
                    "close_change_percent": _number(quote.change_percent),
                    "high": _number(quote.high),
                    "close": _number(quote.close),
                    "pre_close": _number(quote.pre_close),
                    "limit_up_price": _number(quote.limit_up_price),
                    "source": quote.source or "",
                }

    settled = sum(1 for item in samples if item["next_day"] is not None)
    continued = sum(1 for item in samples if (item["next_day"] or {}).get("is_limit_up"))
    return {
        "meta": _meta(session, trade_date, kind=MODEL_MEMORY_DIR_NAME),
        "market": {
            "limit_up_count": len(limit_ups),
            "next_trade_date": None if next_day is None else _iso(next_day),
            #: 「第二天」能算出结果的样本数，与分母一起给：只给比例就是没出处的数字
            "next_day_settled_samples": settled,
            "next_day_limit_up_again": continued,
            "next_day_continuation_rate_percent": hit_rate(continued, settled - continued),
        },
        "stocks": samples,
    }


def build_failure_document(session: Session, trade_date: date) -> dict[str, Any]:
    """误判库：这一天 AI 推荐过、而判据日实际**没封板**的每一条，带它当时说的话。

    三条判定数（命中/未命中/待验证）一起写进去。只有失败清单而不写总账，
    读这份文件的人就不知道失败占多大比例——那正是「失败案例单独存档」这句话
    最容易被误用的地方。
    """
    rows = list(
        session.scalars(
            select(AiPickResult)
            .where(AiPickResult.trade_date == trade_date)
            .order_by(AiPickResult.report_id.asc(), AiPickResult.stock_code.asc())
        ).all()
    )
    counts = {
        VERDICT_HIT: sum(1 for row in rows if row.verdict == VERDICT_HIT),
        VERDICT_MISS: sum(1 for row in rows if row.verdict == VERDICT_MISS),
        VERDICT_PENDING: sum(1 for row in rows if row.verdict == VERDICT_PENDING),
    }
    judge_dates = {
        str((row.payload or {}).get("judge_date") or "")
        for row in rows
        if (row.payload or {}).get("judge_date")
    }
    #: 判据日那两根原始数（前收、涨停价）账本里没存列，从行情表现读——渲染层不改库
    quotes = _quotes_for_codes(
        session,
        [str(row.stock_code) for row in rows],
        [day for day in (_from_iso(text) for text in judge_dates) if day],
    )
    names = _names(session, [str(row.stock_code) for row in rows])
    cases: list[dict[str, Any]] = []
    for row in rows:
        if row.verdict != VERDICT_MISS:
            continue
        judge = _from_iso(str((row.payload or {}).get("judge_date") or ""))
        quote = quotes.get((str(row.stock_code), judge)) if judge else None
        cases.append(
            {
                "report_id": int(row.report_id) if row.report_id else None,
                "model": row.model,
                "said_at": row.recommended_at,
                "code": row.stock_code,
                "name": row.stock_name or names.get(str(row.stock_code), ""),
                "source_chain": row.source,
                "score": _number(row.score),
                #: 它当时看到的证据原值（可追出处的那一半）
                "at_recommend_time": {
                    "max_similarity": _number(row.at_max_similarity),
                    "to_limit_percent": _number(row.at_to_limit_percent),
                    "change_percent": _number(row.at_change_percent),
                    "composite_score": _number(row.at_composite_score),
                    "level": row.at_level,
                    "model_gap": _number(row.at_model_gap),
                },
                #: 它当时说的话（追不出出处的那一半）——这才是「喂失败案例」要喂的东西
                "reason": row.reason,
                "counter_evidence": row.counter_evidence,
                "criteria": row.criteria,
                "actual": {
                    "judge_date": _iso(judge),
                    "is_limit_up": row.actual_is_limit_up,
                    "max_change_percent": _number(row.actual_max_change_percent),
                    "close_change_percent": _number(row.actual_change_percent),
                    "close": _number(row.actual_close),
                    "pre_close": _number(quote.pre_close) if quote else None,
                    "limit_up_price": _number(quote.limit_up_price) if quote else None,
                    "source": row.actual_source,
                },
            }
        )

    settled = counts[VERDICT_HIT] + counts[VERDICT_MISS]
    return {
        "meta": _meta(session, trade_date, kind=FAILURE_LOG_DIR_NAME),
        "summary": {
            "picks": len(rows),
            "hit": counts[VERDICT_HIT],
            "miss": counts[VERDICT_MISS],
            "pending": counts[VERDICT_PENDING],
            "settled": settled,
            #: 分母只有已定判的；一条都没定判时是 None（不是 0%）
            "hit_rate_percent": hit_rate(counts[VERDICT_HIT], counts[VERDICT_MISS]),
            "verdict_rule": "判据 = 推荐日之后库里第一个交易日，daily_quotes.is_limit_up 是否为 1",
        },
        "failure_cases": cases,
    }


def _from_iso(text: str) -> date | None:
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def _write_atomic(path: Path, document: dict[str, Any]) -> int:
    """临时文件 + rename：读侧永远不会看到写了一半的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(path)
    return path.stat().st_size


def export_day(session: Session, trade_date: date, *, root: Path | None = None) -> dict[str, Any]:
    """生成并落盘某一天的两份文件，返回路径、字节数与条数。

    磁盘抖动只记账不抛：这一步在每日流水线里排在同步之后，
    一块写不进去的盘不该把整条流水线报成失败，但「没写成」必须说得出。
    """
    result: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "memory_file": "",
        "memory_bytes": 0,
        "memory_samples": 0,
        "failure_file": "",
        "failure_bytes": 0,
        "failure_cases": 0,
        "picks": 0,
        "error": "",
    }
    try:
        samples = build_sample_document(session, trade_date)
        path = memory_file(trade_date, root)
        result["memory_bytes"] = _write_atomic(path, samples)
        result["memory_file"] = str(path)
        result["memory_samples"] = len(samples["stocks"])

        failures = build_failure_document(session, trade_date)
        path = failure_file(trade_date, root)
        result["failure_bytes"] = _write_atomic(path, failures)
        result["failure_file"] = str(path)
        result["failure_cases"] = len(failures["failure_cases"])
        result["picks"] = failures["summary"]["picks"]
    except OSError as error:
        result["error"] = f"落盘失败：{type(error).__name__}: {error}"
        logger.warning("每日 JSON 快照落盘失败（%s）：%s", trade_date, error)
    except Exception as error:  # noqa: BLE001 - 渲染阶段的任何异常都不能带崩流水线
        result["error"] = f"渲染失败：{type(error).__name__}: {error}"
        logger.exception("每日 JSON 快照渲染失败（%s）", trade_date)
    else:
        logger.info(
            "每日 JSON 已落盘 %s：样本 %s 条（%s 字节）、误判 %s 条（%s 字节）",
            trade_date,
            result["memory_samples"],
            result["memory_bytes"],
            result["failure_cases"],
            result["failure_bytes"],
        )
    return result


def list_exported_days(root: Path | None = None) -> dict[str, Any]:
    """两份目录各自有哪些日子、一共多大，页面与排障都要看这个。"""
    payload: dict[str, Any] = {}
    for key, directory in (
        (MODEL_MEMORY_DIR_NAME, memory_dir(root)),
        (FAILURE_LOG_DIR_NAME, failure_dir(root)),
    ):
        days: list[dict[str, Any]] = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                days.append(
                    {
                        "date": path.stem,
                        "bytes": int(stat.st_size),
                        "written_at": datetime.fromtimestamp(stat.st_mtime).isoformat(
                            timespec="seconds"
                        ),
                    }
                )
        payload[key] = {
            "dir": str(directory),
            "exists": directory.is_dir(),
            "days": days,
            "day_count": len(days),
            "total_bytes": sum(int(item["bytes"]) for item in days),
        }
    return payload


def latest_quote_date(session: Session) -> date | None:
    """库里最新的行情日。这一天就是「第二天结果」的判据日所在。"""
    return session.scalar(select(func.max(DailyQuote.trade_date)))


def run_memory(session: Session, trade_date: date, *, root: Path | None = None) -> dict[str, Any]:
    """对账 + 落盘当天与上一个交易日的两份 JSON，返回这一轮做了什么。

    手动接口和每日流水线调的是**同一个**这一条函数：「手动点一次」与「等自动那一拍」
    不该是两套代码。提交权在调用方手里，本函数不 commit。
    """
    from backend.app.services import ai_picks

    result: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        # 没做成就是 None，不是空对象：页面读到 `{}` 会当成「跑了，0 条」，读到 null 才知道这一步根本没成。
        "reconcile": None,
        "export_today": None,
        "export_previous": None,
        "errors": [],
    }
    try:
        result["reconcile"] = ai_picks.reconcile(session)
    except Exception as error:  # noqa: BLE001 - 对账失败不该带崩流水线
        result["errors"].append(f"对账失败：{type(error).__name__}: {error}")
        logger.exception("AI 建议对账失败（%s）", trade_date)

    #: 今天先写（这一份里的误判清单要靠上面那次对账定过判才有内容），再回填上一个交易日
    for key, day in (("export_today", trade_date), ("export_previous", previous_trade_date(session, trade_date))):
        if day is None:
            continue
        export = export_day(session, day, root=root)
        result[key] = export
        if export.get("error"):
            result["errors"].append(f"{day.isoformat()}：{export['error']}")

    if result["errors"]:
        logger.warning(
            "每日对账与快照这一轮有 %s 项没做成：%s", len(result["errors"]), result["errors"]
        )
    return result


def summary(session: Session) -> dict[str, Any]:
    """给页面读的一段总账：对账账本（读 `ai_picks.summary`，不在这里重写一遍）+ 两份目录 + 缺口说明。"""
    from backend.app.services import ai_picks

    latest = latest_quote_date(session)
    return {
        "check": ai_picks.summary(session),
        "export": list_exported_days(),
        "field_gaps": [
            *(
                _null_counts(session, latest)
                if latest is not None
                else ["库里一条行情都还没有：这两份文件在同步之后才有内容可渲染"]
            ),
            *STATIC_FIELD_GAPS,
        ],
        "latest_quote_date": None if latest is None else latest.isoformat(),
        "disclaimer": DISCLAIMER,
    }
