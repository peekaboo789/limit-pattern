"""滚动历史回测：某一天像不像涨停前，和第二天到底涨停了没有。

回测只回答一个问题——「与已收盘涨停样本的三日形态相似度」与「下一交易日是否涨停」
之间有没有**历史统计关联**。它落下的那份分档发生率是这一轮跑完的账：榜上的概率列、
预测页的档位项、给大模型看的那一条证据，全都原样读这一份，不在下游重新拟合或外推。

因果边界三条，靠结构保证，不靠自觉：

* 样本侧：涨停样本的三日窗口严格早于它自己的涨停日（`include_as_of_date=False`）。
  形态里含着涨停那一天，匹配率就成了自证。
* 候选侧：三日窗口含评估日当天（那是收盘后才能拿到的最后一根），但绝不碰标签日。
* 标签：只读评估日之后最近一个有行情的日子，且只取涨停标记与涨跌幅这两个结果字段。

与首页/盘中扫描是同一把尺子：样本池、剔自身、剔脏行全走 `score_day` 与
`score_components`，回测不另写一份打分。唯一差别是**回测不给样本池补拉历史 K 线**——
一轮回测要逐日重算样本池，补拉会把「跑一次回测」变成「同步一遍全网历史」；
更要紧的是补来的那几根线只在跑的那一刻拿得到，第二次跑的分就和第一次不一样了。
凑不齐三根线的样本照实进 `skipped_samples`，页面数得出缺了多少。
（末尾那三行抽验走的是首页那条路，它可能按需补拉**那一只股票**的三天——有界的一次补拉，
不是一轮回测的全市场补拉，且只在池子条数一致时才真去比数值。）
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable

import numpy as np

from backend.app.algorithms.calibration import (
    brier_score,
    calibration_curve,
    expected_calibration_error,
    log_loss,
)
from backend.app.algorithms.features import build_three_day_window
from backend.app.algorithms.similarity import band_of, score_components
from backend.app.core.clock import SHANGHAI
from backend.app.core.constants import (
    BAND_HIGH,
    BAND_LOW,
    BAND_MID,
    BAND_WEAK,
    COMPARE_WINDOW_TRADING_DAYS,
    MIN_BAND_OBSERVATIONS,
)
from backend.app.core.database import session_scope
from backend.app.core.logging import get_logger
from backend.app.models.tables import BacktestObservation, BacktestRun, DailyQuote, Stock
from backend.app.providers.markets import is_in_scope
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.quality_service import error_pairs
from backend.app.services.retention import quote_dates

logger = get_logger("backtest")

BACKTEST_DISCLAIMER = (
    "本模块检验的是「形态相似度」与「下一交易日涨停」的历史统计关联性。"
    "分档给出的数是已经发生过的频率，不是预测保证，不构成投资建议。"
)

#: 一页明细最多取多少条。回测一天给全市场，几十万行不是一页要显示的东西。
OBSERVATION_PAGE_MAX = 1000
#: 抽验重算几条。全重算等于再跑一遍回测，抽三条足够暴露两套口径漂了。
SPOT_CHECK_SIZE = 3
#: 抽验允许的差。落库保留两位小数，比对也用两位，再大就是在容忍两套算法。
SPOT_TOLERANCE = 0.01


def band_labels() -> tuple[str, ...]:
    """从高到低的档位标签，全部问 `band_of` 要——两处各写一份字符串迟早对不上。"""
    floors = sorted({BAND_HIGH, BAND_MID, BAND_LOW, BAND_WEAK}, reverse=True)
    labels = [band_of(floor) for floor in floors]
    labels.append(band_of(floors[-1] - 0.01))
    return tuple(labels)


def _floors() -> list[float]:
    return sorted({BAND_HIGH, BAND_MID, BAND_LOW, BAND_WEAK}, reverse=True)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _next_day_map(days: list[date]) -> dict[date, date | None]:
    ordered = sorted(set(days))
    return {
        day: (ordered[index + 1] if index + 1 < len(ordered) else None)
        for index, day in enumerate(ordered)
    }


def _cut(bars_by_code: dict[str, Any], day: date) -> dict[str, list]:
    """把全市场那份历史截到 `day`（含）。

    一次 `load_bars_bulk` 读全区间再逐日截，和逐日各读一次的结果是同一份：
    合并优先级按 (代码, 日期) 落，截断与合并不相干。差别只在于回测跑二十天
    要不要把两张表读二十遍。
    """
    return {
        code: [bar for bar in bars if bar.trade_date <= day]
        for code, bars in bars_by_code.items()
    }


def _is_dirty(dirty: set[tuple[str, date]], code: str, days: list[date]) -> bool:
    return any((code, day) in dirty for day in days)


# --- 单日打分（回测、榜上概率、预测池共用的那把尺子）--------------------------


def score_day(
    session,
    day: date,
    *,
    bars_by_code: dict[str, list] | None = None,
    names: dict[str, str] | None = None,
    dirty: set[tuple[str, date]] | None = None,
    top_matches: int = 0,
) -> dict:
    """按「截至 `day` 的已收盘涨停样本池」给当天全市场重算一遍相似度。

    候选集**剔掉当天已涨停的股票**：它们的形态里已经含着涨停那一天，留着就是把结果当特征。
    所以这里出来的榜天然全是「还没涨停、明天才见分晓」的那批。
    脏行（error 级质量异常）无论出现在候选的窗口里还是样本的窗口里，都把那一条整体剔掉——
    用一个坏掉的收盘价算出来的分数不该参与分档统计。
    板块范围与生产路径一致（`is_in_scope`），否则榜上那批股票的概率列就不是同一群样本算出来的。
    """
    dirty = dirty if dirty is not None else error_pairs(session, end=day)
    if bars_by_code is None:
        bars_by_code = load_bars_bulk(session, day)
    if names is None:
        names = {row.code: row.name for row in session.query(Stock)}
    pool_days, samples, skipped_samples, sample_window_ok = limit_up_samples_for_backtest(
        session, day, bars_by_code, dirty
    )

    quotes = (
        session.query(DailyQuote.stock_code, DailyQuote.is_limit_up)
        .filter(DailyQuote.trade_date == day)
        .all()
    )
    codes = sorted({code for code, _ in quotes if is_in_scope(code)})
    limit_up_on_day = {code for code, is_limit in quotes if is_limit}

    targets: list[tuple[str, list[float], list[str]]] = []
    missing_bars = 0
    dirty_skipped = 0
    for code in codes:
        if code in limit_up_on_day:
            continue
        bars = bars_by_code.get(code) or []
        window = build_three_day_window(bars, day, include_as_of_date=True)
        if not window.ok:
            missing_bars += 1
            continue
        window_days = [bar.trade_date for bar in window.days]
        if _is_dirty(dirty, code, [day, *window_days]):
            dirty_skipped += 1
            continue
        targets.append((code, window.vector, [item.isoformat() for item in window_days]))

    items: list[dict] = []
    if targets and samples:
        parts = score_components(
            np.asarray([vector for _code, vector, _days in targets], dtype=float),
            np.asarray([sample["vector"] for sample in samples], dtype=float),
        )
        columns_of: dict[str, list[int]] = {}
        for index, sample in enumerate(samples):
            columns_of.setdefault(sample["code"], []).append(index)
        for row_index, (code, _vector, window_days) in enumerate(targets):
            scores = parts["score"][row_index].astype(float).copy()
            # 涨停那天就在池子里的股票不能和自己比：拿自己的历史比自己永远是满分。
            for column in columns_of.get(code, []):
                scores[column] = -1.0
            valid = scores[scores >= 0.0]
            if valid.size == 0:
                continue
            best = int(np.argmax(scores))
            sample = samples[best]
            matches: list[dict] = []
            if top_matches > 0:
                order = np.argsort(-scores)
                for column in order:
                    if float(scores[column]) < 0.0:
                        break
                    if len(matches) >= int(top_matches):
                        break
                    matched = samples[int(column)]
                    matches.append(
                        {
                            "code": matched["code"],
                            "limit_date": matched["limit_date"].isoformat(),
                            "score": _round(float(scores[column])),
                        }
                    )
            items.append(
                {
                    "code": code,
                    "name": names.get(code, ""),
                    "max_similarity": _round(float(scores[best])),
                    "avg_similarity": _round(float(valid.mean())),
                    "sample_count": int(valid.size),
                    "high_similarity_count": int(np.count_nonzero(valid >= BAND_HIGH)),
                    "band": band_of(float(scores[best])),
                    "window": window_days,
                    "best_match": {
                        "code": sample["code"],
                        "name": names.get(sample["code"], ""),
                        "limit_date": sample["limit_date"].isoformat(),
                        "score": _round(float(scores[best])),
                    },
                    "matches": matches,
                }
            )
    items.sort(key=lambda item: (-(item["max_similarity"] or 0.0), item["code"]))

    return {
        "trade_date": day.isoformat(),
        "pool_dates": [item.isoformat() for item in pool_days],
        "pool_size": len(samples),
        "skipped_samples": skipped_samples,
        "limit_up_count": len(limit_up_on_day),
        "universe_count": len(codes),
        "scanned_count": len(items),
        "missing_bars": missing_bars,
        "dirty_skipped": dirty_skipped,
        "sample_window_ok": sample_window_ok,
        "items": items,
    }


def limit_up_samples_for_backtest(session, day: date, bars_by_code: dict[str, list], dirty: set) -> tuple[list[date], list[dict], list[dict], bool]:
    """回测用的样本池：只用库里已有的行情，不补拉历史，脏样本不进池。

    与盘中/首页那个 `limit_up_samples` 是同一套构窗判据，差的三件事都在这里说明白：
    不补拉（否则同一个区间跑两次得到两套分）、按 error 账剔样本、以及**顺手把窗口判据
    带出去**（`sample_window_ok`）——判据吃的是这里真正产出的那些窗口，不是另跑一遍的结论。
    """
    from backend.app.services.similarity_service import pool_dates

    dates = pool_dates(session, day)
    if not dates:
        return [], [], [], True
    names = {row.code: row.name for row in session.query(Stock.code, Stock.name)}
    rows = (
        session.query(DailyQuote.stock_code, DailyQuote.trade_date)
        .filter(DailyQuote.trade_date.in_(dates), DailyQuote.is_limit_up.is_(True))
        .all()
    )
    #: 涨停日当天就有 error 的那一条不该当成涨停样本。判据必须落到 (代码, 涨停日) 那一条上：
    #: 只按代码封杀的话，600000 在 06-10 有一根坏线，会把它在 06-03、06-08 的两条干净样本
    #: 一起清掉，池子平白少两条。
    samples: list[dict] = []
    skipped: list[dict] = []
    for code, limit_day in rows:
        if not is_in_scope(code):
            continue
        if (code, limit_day) in dirty:
            skipped.append(
                {
                    "code": code,
                    "name": names.get(code, ""),
                    "limit_date": limit_day.isoformat(),
                    "reason": "涨停日当天有 error 级数据质量异常",
                }
            )
            continue
        bars = [bar for bar in (bars_by_code.get(code) or []) if bar.trade_date <= limit_day]
        window = build_three_day_window(bars, limit_day, include_as_of_date=False)
        if not window.ok:
            skipped.append(
                {
                    "code": code,
                    "name": names.get(code, ""),
                    "limit_date": limit_day.isoformat(),
                    "reason": "库里凑不齐涨停前三根K线（回测不补拉历史）",
                }
            )
            continue
        window_days = [bar.trade_date for bar in window.days]
        if _is_dirty(dirty, code, window_days):
            skipped.append(
                {
                    "code": code,
                    "name": names.get(code, ""),
                    "limit_date": limit_day.isoformat(),
                    "reason": "形态窗口里有 error 级数据质量异常",
                }
            )
            continue
        samples.append(
            {
                "code": code,
                "limit_date": limit_day,
                "vector": window.vector,
                "window": [bar.isoformat() for bar in window_days],
            }
        )
    samples.sort(key=lambda item: (item["limit_date"], item["code"]))
    skipped.sort(key=lambda item: (item["limit_date"], item["code"]))
    ok = all(
        all(bar < sample["limit_date"].isoformat() for bar in sample["window"])
        for sample in samples
    )
    return dates, samples, skipped, ok


# --- 统计与校准 ---------------------------------------------------------------


def _bucket_stats(observations: list[dict]) -> tuple[list[dict], float | None]:
    """每一档的观测数、次日涨停次数、实际发生率、相对基准倍数，加上全样本基准。

    发生率只数**有结果**的观测；没揭晓的（区间最后一天那些）进 `pending_count`。
    把没揭晓的当成没涨停，等于用未来的空白压低历史发生率。
    """
    known = [item for item in observations if item["next_limit_up"] is not None]
    baseline = round(sum(1 for item in known if item["next_limit_up"]) / len(known), 6) if known else None
    buckets: list[dict] = []
    for label in band_labels():
        members = [item for item in observations if item["band"] == label]
        settled = [item for item in members if item["next_limit_up"] is not None]
        hits = sum(1 for item in settled if item["next_limit_up"])
        #: 这一档一条有结果的观测都没有时，发生率是「算不出来」而不是 0%——
        #: 页面画成 0% 就是在说「这一档从没涨停过」，那是另一句话。
        rate = round(hits / len(settled), 6) if settled else None
        buckets.append(
            {
                "band": label,
                "sample_count": len(settled),
                "limit_up_count": hits,
                "limit_up_rate": rate,
                "baseline_rate": baseline,
                "lift": round(rate / baseline, 4) if rate is not None and baseline else None,
                "pending_count": len(members) - len(settled),
            }
        )
    return buckets, baseline


def _calibrate(observations: list[dict], *, split_ratio: float) -> dict:
    """按**交易日**切分：前半段拟合「档 → 发生率」，后半段检验。

    切观测而不切日子是作弊：同一天的行情互相高度相关，把 3 月 10 日的观测分到两边，
    检验集里就掺进了训练集那一天的信息。
    """
    labelled = [item for item in observations if item["next_limit_up"] is not None]
    days = sorted({item["trade_date"] for item in labelled})
    empty = {
        "available": False,
        "reason": "",
        "split_ratio": split_ratio,
        "train_days": 0,
        "test_days": 0,
        "train_count": 0,
        "test_count": 0,
        "brier": None,
        "log_loss": None,
        "ece": None,
        "bins": [],
        "band_probabilities": {},
        "band_checks": [],
    }
    if len(days) < 2:
        return {**empty, "reason": "有结果的交易日不足 2 天，无法按时间切分"}
    if len(labelled) < 20:
        return {**empty, "reason": f"有结果的观测仅 {len(labelled)} 条，样本量不足以校准"}
    cut = max(1, min(int(len(days) * split_ratio), len(days) - 1))
    train_days, test_days = set(days[:cut]), set(days[cut:])
    train = [item for item in labelled if item["trade_date"] in train_days]
    test = [item for item in labelled if item["trade_date"] in test_days]
    if not train or not test:
        return {**empty, "reason": "切分后前半段或后半段是空的"}

    train_baseline = sum(1 for item in train if item["next_limit_up"]) / len(train)
    probabilities: dict[str, float] = {}
    for label in {item["band"] for item in train}:
        members = [item for item in train if item["band"] == label]
        probabilities[label] = sum(1 for item in members if item["next_limit_up"]) / len(members)

    predicted = [probabilities.get(item["band"], train_baseline) for item in test]
    outcomes = [1 if item["next_limit_up"] else 0 for item in test]
    curve = calibration_curve(predicted, outcomes, bins=10)
    band_checks = []
    for label in sorted({item["band"] for item in test}):
        members = [item for item in test if item["band"] == label]
        hits = sum(1 for item in members if item["next_limit_up"])
        band_checks.append(
            {
                "band": label,
                "predicted": round(probabilities.get(label, train_baseline), 6),
                "observed": round(hits / len(members), 6),
                "count": len(members),
            }
        )
    return {
        "available": True,
        "reason": "",
        "split_ratio": split_ratio,
        "train_days": len(train_days),
        "test_days": len(test_days),
        "train_count": len(train),
        "test_count": len(test),
        "brier": brier_score(predicted, outcomes),
        "log_loss": log_loss(predicted, outcomes),
        "ece": expected_calibration_error(predicted, outcomes),
        "bins": curve,
        "band_probabilities": {key: round(value, 6) for key, value in probabilities.items()},
        "band_checks": band_checks,
    }


def _leakage(day_rows: list[dict], observations: list[dict], session) -> dict:
    """四条结构判据 + 一条抽验重算。哪一条不成立就把那一条说出来，不合并成一句「通过」。"""
    window_upto = all(
        item["window"] and item["window"][-1] <= item["trade_date"] for item in observations
    )
    excludes_label = all(
        item["next_date"] is None
        or (item["window"] and item["window"][-1] < item["next_date"])
        for item in observations
    )
    label_after = all(
        item["next_date"] is None or item["next_date"] > item["trade_date"] for item in observations
    )
    sample_before = all(row["sample_window_ok"] for row in day_rows)
    spots = _spot_check(observations, day_rows, session)
    spot_ok = all(check["ok"] is not False for check in spots)
    passed = bool(window_upto and excludes_label and label_after and sample_before and spot_ok)
    detail = (
        "全部观测的三日窗口不晚于评估日、且不碰标签日，标签日严格晚于评估日，"
        "涨停样本窗口严格早于涨停日，抽验重算与落库相似度一致"
        if passed
        else "；".join(
            text
            for flag, text in (
                (window_upto, "存在形态日晚于评估日的观测"),
                (excludes_label, "存在三日窗口碰到标签日的观测"),
                (label_after, "存在标签日不晚于评估日的观测"),
                (sample_before, "存在涨停样本窗口不早于涨停日的样本"),
                (spot_ok, "抽验重算与落库相似度不一致"),
            )
            if not flag
        )
    )
    return {
        "passed": passed,
        "window_upto_day": window_upto,
        "window_excludes_label": excludes_label,
        "label_after_day": label_after,
        "sample_window_before_limit": sample_before,
        "spot_checks": spots,
        "detail": detail,
        "days": len(day_rows),
    }


def _spot_check(observations: list[dict], day_rows: list[dict], session, sample_size: int = SPOT_CHECK_SIZE) -> list[dict]:
    """抽几条走首页那条路重算：分数必须和落库的一致。

    这一步是「回测记录值 == 生产路径当日重算值」的证据。两条路一旦漂了，
    页面上那个概率列就不再是榜上那批股票的档位发生率。

    两条路的样本池本来就有两处**已知且刻意**的差别（回测不补拉、回测剔 error 样本），
    所以先比池子条数：条数不同就只登记、不判红——那种情况下数值本就该不同，
    硬比出来的红只会把「口径差别」说成「算法坏了」。条数一致而分数不一致，才是尺子漂了。
    """
    if not observations or sample_size <= 0:
        return []
    from backend.app.services.similarity_service import similar_to

    pool_by_day = {row["trade_date"]: int(row["pool_size"]) for row in day_rows}
    step = max(1, len(observations) // int(sample_size))
    picked = [observations[index] for index in range(0, len(observations), step)][: int(sample_size)]
    checks: list[dict] = []
    for item in picked:
        check = {
            "stock_code": item["stock_code"],
            "trade_date": item["trade_date"],
            "recorded": item["max_similarity"],
            "recomputed": None,
            "backtest_pool": pool_by_day.get(item["trade_date"]),
            "production_pool": None,
            "compared": False,
            "ok": None,
            "reason": "",
        }
        try:
            result = similar_to(session, item["stock_code"], date.fromisoformat(item["trade_date"]), 5)
        except LookupError as exc:
            # 回测能给这一行打分，首页那条路却说凑不齐三天——两条路读的不是同一份行情。
            check["ok"] = False
            check["reason"] = f"按首页那条路重算失败：{exc}"
            checks.append(check)
            continue
        check["production_pool"] = int(result.get("pool_size") or 0)
        if check["production_pool"] != check["backtest_pool"]:
            check["reason"] = (
                f"样本池条数不同（回测 {check['backtest_pool']} 条、首页那条路 {check['production_pool']} 条），"
                "数值不比对"
            )
            checks.append(check)
            continue
        scores = [row["score"] for row in result.get("matches") or [] if row.get("score") is not None]
        if not scores:
            check["ok"] = False
            check["reason"] = "首页那条路当天没有可比样本，回测却给这一行打了分"
            checks.append(check)
            continue
        recomputed = _round(max(scores))
        check["recomputed"] = recomputed
        check["compared"] = True
        check["ok"] = bool(
            recomputed is not None
            and item["max_similarity"] is not None
            and abs(recomputed - float(item["max_similarity"])) <= SPOT_TOLERANCE
        )
        if not check["ok"]:
            check["reason"] = f"重算 {recomputed} 与落库 {item['max_similarity']} 相差超过 {SPOT_TOLERANCE}"
        checks.append(check)
    return checks


# --- 主流程 -------------------------------------------------------------------


def run_backtest(
    session,
    *,
    start: date,
    end: date,
    split_ratio: float = 0.5,
    max_days: int | None = None,
    on_progress: Callable[[int, int, date], None] | None = None,
) -> dict:
    """区间内逐日重算样本池、逐日统计次日结果，最后汇成一轮回测账。

    明细在返回值的 `observations` 里，由 `persist_run` 落库。
    代价随区间长度线性增长：一天等于一轮全市场打分。区间长了就先跑短一段。
    """
    if start > end:
        raise ValueError(f"起始日 {start} 晚于结束日 {end}")
    if not 0.1 <= split_ratio <= 0.9:
        raise ValueError(f"切分比例要落在 0.1~0.9，当前 {split_ratio}")
    all_days = quote_dates(session)
    days = [day for day in all_days if start <= day <= end]
    if not days:
        raise LookupError(f"{start}~{end} 区间内没有任何已同步行情")
    if max_days:
        days = days[: int(max_days)]

    next_map = _next_day_map(all_days)
    # 窗口最长往前看三天，样本池再往前看三个交易日：脏行那份账往区间下界之前多取几天，
    # 否则区间第一天的形态窗口里住着一条 error 也照用。
    dirty = error_pairs(
        session,
        start=days[0] - timedelta(days=COMPARE_WINDOW_TRADING_DAYS * 2 + 4),
        end=days[-1],
    )
    bars_by_code = load_bars_bulk(session, days[-1])
    names = {row.code: row.name for row in session.query(Stock.code, Stock.name)}

    observations: list[dict] = []
    day_rows: list[dict] = []
    total = len(days)
    for index, day in enumerate(days, start=1):
        if on_progress is not None:
            on_progress(index, total, day)
        scored = score_day(
            session,
            day,
            bars_by_code=_cut(bars_by_code, day),
            names=names,
            dirty=dirty,
            top_matches=0,
        )
        next_day = next_map.get(day)
        labels = _labels_on(session, next_day)
        day_rows.append(
            {
                "trade_date": day.isoformat(),
                "limit_up_count": scored["limit_up_count"],
                "pool_size": scored["pool_size"],
                "pool_dates": scored["pool_dates"],
                "skipped_samples": len(scored["skipped_samples"]),
                "universe_count": scored["universe_count"],
                "scanned_count": scored["scanned_count"],
                "missing_bars": scored["missing_bars"],
                "dirty_skipped": scored["dirty_skipped"],
                "sample_window_ok": scored["sample_window_ok"],
                "next_trade_date": _iso(next_day),
            }
        )
        for item in scored["items"]:
            outcome = labels.get(item["code"])
            observations.append(
                {
                    "trade_date": day.isoformat(),
                    "stock_code": item["code"],
                    "stock_name": item["name"],
                    "max_similarity": item["max_similarity"],
                    "avg_similarity": item["avg_similarity"],
                    "sample_count": item["sample_count"],
                    "high_similarity_count": item["high_similarity_count"],
                    "band": item["band"],
                    "window": item["window"],
                    "next_date": _iso(next_day),
                    "next_limit_up": None if outcome is None else outcome[0],
                    "next_change_percent": None if outcome is None else outcome[1],
                }
            )
        logger.info(
            "回测 %s：样本 %s 条、跳过样本 %s 条、扫描 %s 只、缺线 %s 只、脏行剔除 %s 只",
            day,
            day_rows[-1]["pool_size"],
            day_rows[-1]["skipped_samples"],
            day_rows[-1]["scanned_count"],
            day_rows[-1]["missing_bars"],
            day_rows[-1]["dirty_skipped"],
        )

    buckets, baseline = _bucket_stats(observations)
    calibration = _calibrate(observations, split_ratio=split_ratio)
    leakage = _leakage(day_rows, observations, session)
    return {
        "start_date": days[0].isoformat(),
        "end_date": days[-1].isoformat(),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "split_ratio": split_ratio,
        "day_count": len(days),
        "observation_count": len(observations),
        "baseline_rate": baseline,
        "days": day_rows,
        "buckets": buckets,
        "calibration": calibration,
        "leakage": leakage,
        "observations": observations,
        "parameters": {
            "compare_window_trading_days": COMPARE_WINDOW_TRADING_DAYS,
            "band_floors": _floors(),
            "high_band": BAND_HIGH,
            "min_band_observations": MIN_BAND_OBSERVATIONS,
            "max_days": max_days,
        },
        "disclaimer": BACKTEST_DISCLAIMER,
    }


def _labels_on(session, day: date | None) -> dict[str, tuple[bool, float | None]]:
    """结果标签：这一天涨停了没有、涨了多少。全项目唯一一处读取评估日之后数据的地方。"""
    if day is None:
        return {}
    rows = (
        session.query(DailyQuote.stock_code, DailyQuote.is_limit_up, DailyQuote.change_percent)
        .filter(DailyQuote.trade_date == day)
        .all()
    )
    return {code: (bool(is_limit_up), None if change is None else float(change)) for code, is_limit_up, change in rows}


# --- 落库与读取 ---------------------------------------------------------------


def persist_run(session, payload: dict, *, run_id: int | None = None) -> int:
    """把一轮回测账和它的明细写进库，返回 `run_id`。

    `run_id` 给了就**改写那一行**（后台任务开跑前先写的 `running` 占位），没给就新插一行。
    调用方负责 commit（走 `session_scope` 就自动提交）；直接调本函数的（测试、脚本）
    得到一行干净的 done。
    """
    observations = payload.get("observations") or []
    run = session.get(BacktestRun, int(run_id)) if run_id is not None else None
    if run is None:
        run = BacktestRun()
        session.add(run)
    run.start_date = date.fromisoformat(payload["start_date"])
    run.end_date = date.fromisoformat(payload["end_date"])
    run.split_ratio = float(payload["split_ratio"])
    run.status = "done"
    run.result = {
        "days": payload["days"],
        "buckets": payload["buckets"],
        "calibration": payload["calibration"],
        "leakage": payload["leakage"],
        "baseline_rate": payload["baseline_rate"],
        "requested_start": payload.get("requested_start"),
        "requested_end": payload.get("requested_end"),
        "disclaimer": payload["disclaimer"],
    }
    run.day_count = int(payload["day_count"])
    run.observation_count = int(payload["observation_count"])
    run.baseline_rate = payload["baseline_rate"]
    run.note = (_run_note(payload) or "")[:200]
    run.error = ""
    run.parameters = payload.get("parameters") or {}
    run.finished_at = datetime.now(SHANGHAI).replace(tzinfo=None)
    session.flush()
    for index, item in enumerate(observations):
        session.add(
            BacktestObservation(
                run_id=int(run.id),
                trade_date=date.fromisoformat(item["trade_date"]),
                stock_code=item["stock_code"],
                stock_name=item["stock_name"] or "",
                max_similarity=float(item["max_similarity"] or 0.0),
                avg_similarity=item["avg_similarity"],
                sample_count=int(item["sample_count"] or 0),
                high_similarity_count=int(item["high_similarity_count"] or 0),
                band=item["band"],
                next_date=date.fromisoformat(item["next_date"]) if item["next_date"] else None,
                next_limit_up=item["next_limit_up"],
                next_change_percent=item["next_change_percent"],
            )
        )
        if index % 1000 == 999:
            session.flush()
    session.flush()
    return int(run.id)


def _run_note(payload: dict) -> str:
    """一句话账目：区间多长、多少条观测、防泄漏自检过没过。跑砸了要看的是这行。"""
    leakage = payload.get("leakage") or {}
    state = "防泄漏自检通过" if leakage.get("passed") else f"防泄漏自检未通过：{leakage.get('detail')}"
    calibration = payload.get("calibration") or {}
    calib = (
        f"校准 ECE {calibration.get('ece')}"
        if calibration.get("available")
        else f"未校准（{calibration.get('reason') or '原因未记'}）"
    )
    return (
        f"{payload['day_count']} 个交易日、{payload['observation_count']} 条观测，"
        f"基准发生率 {_percent_text(payload.get('baseline_rate'))}；{calib}；{state}"
    )


def _percent_text(rate: float | None) -> str:
    return "无从谈起" if rate is None else f"{round(rate * 100.0, 2)}%"


def latest_done_run(session) -> BacktestRun | None:
    """最近一次**已完成**的回测。`running` 与 `failed` 不会被下游读到。

    按 `created_at` 排（同一秒再按 `id`）而不是按 `id`：回测页可以补跑一段更早的区间，
    那种行的 `id` 更大，但它才是这一列此刻的出处。
    """
    return (
        session.query(BacktestRun)
        .filter(BacktestRun.status == "done")
        .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
        .first()
    )


def band_stats(session) -> tuple[dict[str, dict], BacktestRun | None]:
    """最近一次 done 回测的分档表，原样取用，不在这里重新拟合任何东西。

    预测页的档位项和盘中榜的概率列都从这里取：两处各写一份查询迟早漂成两个数，
    同一只股票在两个页面一个有概率、一个没有，用户只会认为系统在编。
    """
    run = latest_done_run(session)
    if run is None:
        return {}, None
    bands: dict[str, dict] = {}
    for bucket in (run.result or {}).get("buckets") or []:
        if not isinstance(bucket, dict) or "band" not in bucket:
            logger.warning("回测分档记录字段不完整，跳过这一档：%s", bucket)
            continue
        bands[str(bucket["band"])] = bucket
    return bands, run


def probability_table(session) -> dict:
    """盘中榜那一列的整张底表：一次扫描读一次，榜上每一行从这一份取值。

    为什么是一张表而不是一行一次查询：一张 200 行的榜查 200 次同一行记录纯属自找麻烦；
    更要紧的是**一轮榜里所有行必须来自同一次回测**——读到一半有人跑完一次新回测，
    榜上前半是旧发生率、后半是新发生率，这一列就没有可比性了。
    """
    bands, run = band_stats(session)
    floors = _floors()
    if run is None:
        return {
            "rates": {},
            "floors": floors,
            "run_id": None,
            "start_date": None,
            "end_date": None,
            "day_count": 0,
            "observation_count": 0,
            "baseline_percent": None,
            "min_band_observations": MIN_BAND_OBSERVATIONS,
            "note": (
                "还没有任何已完成的回测，档位历史发生率给不出来，概率列整列留空（不是 0%）。"
                "先在回测页跑一次。"
            ),
        }
    rates = {
        band: {
            "percent": None if bucket.get("limit_up_rate") is None else round(float(bucket["limit_up_rate"]) * 100.0, 2),
            "samples": int(bucket.get("sample_count") or 0),
            "lift": None if bucket.get("lift") is None else float(bucket["lift"]),
        }
        for band, bucket in bands.items()
    }
    thin = sorted(band for band, hit in rates.items() if hit["samples"] < MIN_BAND_OBSERVATIONS)
    note = (
        f"概率 = 第 {run.id} 次回测（{run.start_date}~{run.end_date}，{run.day_count} 个交易日、"
        f"{run.observation_count} 条观测，全样本基准发生率 {_percent_text(run.baseline_rate)}）里"
        "同一相似度档的次日实际涨停发生率"
    )
    if thin:
        note = (
            f"{note}；其中 {'、'.join(thin)} 档观测不足 {MIN_BAND_OBSERVATIONS} 条，视为噪声，"
            "落在这些档的行概率留空"
        )
    if not rates:
        #: 跑过、但一档统计都没落下来的回测最容易骗人：`run_id` 是真的，页面会说
        #: 「基于第 N 次回测」，可整列还是空的。这一句把它挑明。
        note = (
            f"最近一次回测（第 {run.id} 次，{run.start_date}~{run.end_date}）"
            "没有任何分档统计（多半是那几天建不出涨停样本池），概率列整列留空，不是 0%"
        )
    return {
        "rates": rates,
        "floors": floors,
        "run_id": run.id,
        "start_date": _iso(run.start_date),
        "end_date": _iso(run.end_date),
        "day_count": int(run.day_count or 0),
        "observation_count": int(run.observation_count or 0),
        "baseline_percent": None if run.baseline_rate is None else round(float(run.baseline_rate) * 100.0, 2),
        "min_band_observations": MIN_BAND_OBSERVATIONS,
        "note": note,
    }


def probability_for(table: dict, max_similarity: float | None) -> tuple[str, float | None, int]:
    """这一行的 (档位, 概率百分数或 None, 该档观测数)。

    三种留空分开算但结果一样：这一档观测数不够门槛、这一档在回测里没出现过、
    整张表是空的（没跑过回测）——都是 None，由页面显示「—」。
    `0.00%` 是一个有内容的结论（这一档历史上从没涨停过），不能拿来冒充「不知道」。
    """
    if max_similarity is None:
        return "", None, 0
    label = band_of(float(max_similarity))
    hit = (table.get("rates") or {}).get(label)
    if not isinstance(hit, dict):
        return label, None, 0
    samples = int(hit.get("samples") or 0)
    if samples < int(table.get("min_band_observations") or MIN_BAND_OBSERVATIONS):
        return label, None, samples
    return label, hit.get("percent"), samples


def run_payload(run: BacktestRun) -> dict:
    result = dict(run.result or {})
    return {
        "run_id": run.id,
        "start_date": _iso(run.start_date),
        "end_date": _iso(run.end_date),
        "status": run.status,
        "note": run.note or "",
        "error": run.error or "",
        "split_ratio": run.split_ratio,
        "day_count": int(run.day_count or 0),
        "observation_count": int(run.observation_count or 0),
        "baseline_rate": run.baseline_rate,
        "created_at": _iso_dt(run.created_at),
        "finished_at": _iso_dt(run.finished_at),
        "parameters": run.parameters or {},
        "days": result.get("days") or [],
        "buckets": result.get("buckets") or [],
        "calibration": result.get("calibration") or {},
        "leakage": result.get("leakage") or {},
        "disclaimer": result.get("disclaimer") or BACKTEST_DISCLAIMER,
    }


def _iso_dt(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def list_runs(session, *, limit: int = 20) -> list[dict]:
    rows = session.query(BacktestRun).order_by(BacktestRun.id.desc()).limit(int(limit)).all()
    return [
        {
            "run_id": run.id,
            "start_date": _iso(run.start_date),
            "end_date": _iso(run.end_date),
            "status": run.status,
            "note": run.note or "",
            "error": run.error or "",
            "day_count": int(run.day_count or 0),
            "observation_count": int(run.observation_count or 0),
            "baseline_rate": run.baseline_rate,
            "split_ratio": run.split_ratio,
            "created_at": _iso_dt(run.created_at),
            "finished_at": _iso_dt(run.finished_at),
        }
        for run in rows
    ]


def observation_page(session, run_id: int, *, band: str | None = None, code: str | None = None, limit: int = 100, offset: int = 0) -> dict:
    query = session.query(BacktestObservation).filter(BacktestObservation.run_id == run_id)
    if band:
        query = query.filter(BacktestObservation.band == band)
    if code:
        query = query.filter(BacktestObservation.stock_code == code)
    total = int(query.count())
    rows = (
        query.order_by(BacktestObservation.max_similarity.desc(), BacktestObservation.trade_date.desc())
        .offset(int(offset))
        .limit(min(int(limit), OBSERVATION_PAGE_MAX))
        .all()
    )
    return {
        "run_id": run_id,
        "band": band or "",
        "stock_code": code or "",
        "total": total,
        "limit": min(int(limit), OBSERVATION_PAGE_MAX),
        "offset": int(offset),
        "items": [
            {
                "trade_date": _iso(row.trade_date),
                "stock_code": row.stock_code,
                "stock_name": row.stock_name or "",
                "max_similarity": row.max_similarity,
                "avg_similarity": row.avg_similarity,
                "sample_count": int(row.sample_count or 0),
                "high_similarity_count": int(row.high_similarity_count or 0),
                "band": row.band,
                "next_date": _iso(row.next_date),
                "next_limit_up": row.next_limit_up,
                "next_change_percent": row.next_change_percent,
            }
            for row in rows
        ],
    }


def delete_run(session, run_id: int) -> dict:
    """删一轮回测账和它的明细。这是派生数据，随时可以重跑，不碰原始行情。"""
    run = session.get(BacktestRun, run_id)
    if run is None:
        return {"ok": False, "reason": f"没有第 {run_id} 次回测"}
    deleted = (
        session.query(BacktestObservation)
        .filter(BacktestObservation.run_id == run_id)
        .delete(synchronize_session=False)
    )
    session.delete(run)
    session.flush()
    return {"ok": True, "run_id": run_id, "deleted_observations": int(deleted or 0)}


# --- 后台任务：一轮回测就是一次长任务 -----------------------------------------

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "running": False,
    "progress": 0,
    "phase": "idle",
    "message": "还没有跑过回测",
    "task_id": "",
    "days_done": 0,
    "days_total": 0,
    "current_date": "",
    "error": "",
    "run_id": None,
    "last_finished_at": "",
}


def reset_state() -> None:
    """测试收尾用：清状态，不等那个 daemon 线程。"""
    with _LOCK:
        _STATE.update(
            running=False,
            progress=0,
            phase="idle",
            message="还没有跑过回测",
            task_id="",
            days_done=0,
            days_total=0,
            current_date="",
            error="",
            run_id=None,
        )


def status() -> dict:
    with _LOCK:
        return dict(_STATE)


def default_range(session) -> tuple[date, date]:
    days = quote_dates(session)
    if not days:
        raise LookupError("库里没有任何行情，跑不了回测")
    return days[0], days[-1]


def retire_stale_runs(session) -> int:
    """把上一个进程留下的 `running` 行标成 failed。

    后台线程是 daemon，进程一退就没了收尾的人；那种行留在库里等于永远「正在回测」。
    只在**能确定本进程没有在跑**的时候调它：启动时、以及 `start` 拿到 `_STATE` 的
    「没在跑」之后。返回改了几行。
    """
    rows = session.query(BacktestRun).filter(BacktestRun.status == "running").all()
    if not rows:
        return 0
    moment = datetime.now(SHANGHAI).replace(tzinfo=None)
    for run in rows:
        run.status = "failed"
        run.error = "进程重启，这一轮没有跑完"
        run.note = "跑砸了：进程重启，这一轮没有跑完"[:200]
        run.finished_at = moment
    session.flush()
    return len(rows)


def _new_run(session, *, low: date, high: date, split_ratio: float, max_days: int | None) -> int:
    """后台任务开跑之前先落一行 `running`，返回它的 `run_id`。

    和真正的回测**分开提交**：跑砸了那一行要留下来当账，不能被同一笔事务回滚掉。
    页面在跑的那几分钟据此说得出「库里有一轮在跑、跑的是哪一段」。
    """
    run = BacktestRun(
        start_date=low,
        end_date=high,
        split_ratio=float(split_ratio),
        status="running",
        result={},
        day_count=0,
        observation_count=0,
        baseline_rate=None,
        note=f"{low}~{high} 正在回测"[:200],
        error="",
        parameters={
            "compare_window_trading_days": COMPARE_WINDOW_TRADING_DAYS,
            "band_floors": _floors(),
            "high_band": BAND_HIGH,
            "min_band_observations": MIN_BAND_OBSERVATIONS,
            "max_days": max_days,
        },
        finished_at=None,
    )
    session.add(run)
    session.flush()
    return int(run.id)


def _fail_run(session, run_id: int, exc: BaseException) -> None:
    """把占位行改成 `failed` 并写下原因。

    这一行不是给下游读的（概率列只认 `done`），它是「跑砸了」的账：哪一段区间、什么错。
    没有它，回测页就只剩一张空表，像什么都没发生过。
    """
    run = session.get(BacktestRun, int(run_id))
    if run is None:  # 这一轮被用户在跑的时候删掉了
        return
    reason = (str(exc) or type(exc).__name__)[:500]
    run.status = "failed"
    run.error = reason
    run.note = f"跑砸了：{reason}"[:200]
    run.finished_at = datetime.now(SHANGHAI).replace(tzinfo=None)
    session.flush()


def start(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    split_ratio: float = 0.5,
    max_days: int | None = None,
) -> dict:
    """起一轮回测。已经有一轮在跑就接着等那一份，不起第二个线程。

    校验在起线程**之前**做完：区间不合法、库里没行情这些是要当场回答的，
    不该变成一个跑完才说的后台错误。
    """
    with _LOCK:
        if _STATE["running"]:
            return {"task_id": str(_STATE["task_id"]), "already_running": True, **_public()}
    with session_scope() as session:
        low, high = (start_date, end_date)
        if low is None or high is None:
            lib_low, lib_high = default_range(session)
            low = low or lib_low
            high = high or lib_high
        if low > high:
            raise ValueError(f"起始日 {low} 晚于结束日 {high}")
        if not 0.1 <= float(split_ratio) <= 0.9:
            raise ValueError(f"切分比例要落在 0.1~0.9，当前 {split_ratio}")
        days = [day for day in quote_dates(session) if low <= day <= high]
    if not days:
        raise LookupError(f"{low}~{high} 区间内没有任何已同步行情")
    if max_days:
        days = days[: int(max_days)]
    task_id = uuid.uuid4().hex[:12]
    with session_scope() as session:
        #: 走到这里已经确认本进程没有在跑，库里还挂着 running 的只可能是上一进程的残行。
        retire_stale_runs(session)
        run_id = _new_run(session, low=low, high=high, split_ratio=float(split_ratio), max_days=max_days)
    with _LOCK:
        _STATE.update(
            running=True,
            progress=1,
            phase="running",
            message=f"正在回测 {low}~{high}（{len(days)} 个交易日）",
            task_id=task_id,
            days_done=0,
            days_total=len(days),
            current_date="",
            error="",
            run_id=run_id,
        )
    thread = threading.Thread(
        target=_run,
        args=(task_id, run_id, low, high, float(split_ratio), max_days),
        name="lp-backtest",
        daemon=True,
    )
    thread.start()
    return {"task_id": task_id, "already_running": False, "days_total": len(days), **_public()}


def _public() -> dict:
    return {
        "running": bool(_STATE["running"]),
        "progress": int(_STATE["progress"]),
        "message": str(_STATE["message"]),
        "run_id": _STATE["run_id"],
    }


def _run(
    task_id: str,
    run_id: int,
    low: date,
    high: date,
    split_ratio: float,
    max_days: int | None,
) -> None:
    def on_progress(index: int, total: int, day: date) -> None:
        with _LOCK:
            _STATE.update(
                progress=min(94, int(index / max(1, total) * 90) + 2),
                days_done=index,
                days_total=total,
                current_date=day.isoformat(),
                message=f"正在回测 {day}（第 {index}/{total} 天）",
            )

    try:
        with session_scope() as session:
            payload = run_backtest(
                session,
                start=low,
                end=high,
                split_ratio=split_ratio,
                max_days=max_days,
                on_progress=on_progress,
            )
            with _LOCK:
                _STATE.update(progress=95, phase="persist", message="正在写回测账与明细")
            persist_run(session, payload, run_id=run_id)
            leakage = payload.get("leakage") or {}
            if not leakage.get("passed"):
                logger.error("回测防泄漏自检未通过：%s", leakage.get("detail"))
        moment = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        with _LOCK:
            _STATE.update(
                running=False,
                progress=100,
                phase="idle",
                message=f"回测完成：{payload['day_count']} 天、{payload['observation_count']} 条观测",
                run_id=run_id,
                last_finished_at=moment,
                error="",
            )
    except Exception as exc:  # noqa: BLE001 - 后台任务的失败必须变成状态里的一句话
        logger.exception("回测失败：%s", exc)
        reason = str(exc) or type(exc).__name__
        try:
            with session_scope() as session:
                _fail_run(session, run_id, exc)
        except Exception:  # noqa: BLE001 - 连失败账都写不进去，状态里那句更不能少
            logger.exception("回测失败，且写失败账时又炸了一次")
        with _LOCK:
            _STATE.update(
                running=False,
                progress=0,
                phase="idle",
                message=f"回测失败：{reason}",
                error=reason,
                #: 失败的行也留在 `run_id` 上：页面据此指到台账里那一条，
                #: 而不是只给一句「失败了」让人自己去猜是哪一段。
                run_id=run_id,
            )


__all__ = [
    "BACKTEST_DISCLAIMER",
    "band_labels",
    "band_stats",
    "delete_run",
    "latest_done_run",
    "limit_up_samples_for_backtest",
    "list_runs",
    "observation_page",
    "persist_run",
    "probability_for",
    "probability_table",
    "reset_state",
    "retire_stale_runs",
    "run_backtest",
    "run_payload",
    "score_day",
    "start",
    "status",
]
