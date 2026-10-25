"""次日预测池：今天像涨停前、明天才见分晓的那批股票，按三项历史证据排序。

和 legacy 那份的区别只有一处：**榜是从库里现算的，不是读 `pattern_matches`**。
精简版那张表此刻还没有写入方（档3 才落库），而这一页要的「截至今天的最高匹配度」
本来就有唯一一把尺子——`backtest_service.score_day`。回测的观测、盘中榜的概率列、
这一页的挑选池全都吃它，才不会漂出三套相似度。

三条边界，逐条都有对应的判据：

* 候选不含评估日已涨停的股票（`score_day` 剔的），所以池子天然全是「还没涨停」的那批。
* 连板证据只允许查「样本涨停日的次日**严格早于**基准日」的结果——等于或晚于基准日的一律
  连查都不查。这是本模块唯一的红线，测试对着它跑负向控制。盘中监控的 AI 分析也调
  `carry_evidence` 这一个方法，不另写一份取数，免得两处闸门哪天漂了：一边查了未来、一边没查。
* 档位发生率原样读最近一次**已完成**回测的分档表，观测数不足 `MIN_BAND_OBSERVATIONS` 的档
  不参与打分（该项退场，权重摊给其余项，页面写明原因）。

**这一页有两列分数，只有一个叫综合评分。**
名次按下面那三项历史证据的加权排（`prediction_score`，页面列名「预测加权分」）——它是预测语义的排序，
吃的是相似案例后来怎么样了。另有一列「综合评分」，是 `score_model.score_of` 那个全app唯一的算式
（形态/技术/量能/资金/历史成功率五项加权，再扣风险），由 `row_scoring.score_row` 现算，
与盘中监控、AI 榜、个股详情上的同一个数一字不差。**它不参与排序**，只是让这一页也能看到
「这只股票按本系统那把尺子打几分」。谁以后想把加权分也叫「综合」，先问一遍这句话还成不成立。

三项证据（都归一到 0~100 再加权）：

1. `shape` = 当日最高相似度（0~100）——形态像不像。
2. `carry` = 相似度加权的**样本次日连板率**：与它最像的那批涨停样本，涨停之后第二个交易日
   继续涨停的比例。衡量匹配到的案例是「一日游」还是「真强势」。
3. `band` = 档位历史次日涨停发生率相对全样本基准的倍数，按 `PREDICTION_LIFT_CAP` 封顶
   （再高多半是几十条样本抖出来的）。

一个已知后果要挑明：样本池只有「对比窗口」那几个交易日（默认 3），而闸门要求「样本涨停日的次日早于基准日」，
所以基准日**当天**涨停的那批样本永远进不了连板统计（它们的次日就是基准日）。
真正带得出连板证据的只有更早几天涨停的样本（窗口为 1 时一条都没有），`carry_known_count` 为 0 的行会照实显示。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import and_, select

from backend.app.core.constants import (
    DEFAULT_POOL_SIZE,
    MAX_POOL_SIZE,
    MIN_BAND_OBSERVATIONS,
    PREDICTION_DISCLAIMER,
    PREDICTION_FORMULA,
    PREDICTION_LIFT_CAP,
    PREDICTION_WEIGHT_CARRY,
    PREDICTION_WEIGHT_LIFT,
    PREDICTION_WEIGHT_SHAPE,
)
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyQuote, Stock, TradingDay
from backend.app.providers.markets import BOARD_LABELS, classify
from backend.app.services import ai_picks
from backend.app.services import row_scoring
from backend.app.services.backtest_service import band_stats, score_day
from backend.app.services.retention import quote_dates
from backend.app.services.score_model import formula_text

logger = get_logger("prediction")


def prediction_formula() -> str:
    """这一页底部那一句口径：两个分数各讲一遍，综合评分那段现读 `score_model` 生成。

    综合评分那五项的权重、风险扣分与等级线都在 `formula_text()` 里，这里不许抄一份；
    对比窗口一改，这句话跟着变。
    """
    return f"{PREDICTION_FORMULA} 综合评分（同一行的另一列，不参与排序）：{formula_text()}"


#: 每只股票取前 K 个匹配样本作连板证据。榜上的 `matches` 就带这几条，
#: 不再回库里捞第二遍——捞第二遍等于承认这把尺子算出来的邻居不可信。
EVIDENCE_MATCH_LIMIT = 20

PREDICTION_DISCLAIMER_TEXT = (
    "预测分是「形态相似度 + 相似案例的后续表现」的历史统计加权，不是涨停概率，"
    "更不是收益预测。" + PREDICTION_DISCLAIMER
)


@dataclass
class CarryEvidence:
    """Top K 匹配样本的连板累计器。权重就是相似度分数本身：90 分的样本比 65 分的说话更响。

    公开是有意的——盘中监控的 AI 综合分析要用同一个连板率，两处各算一遍迟早会漂。
    """

    weight: float = 0.0
    hit_weight: float = 0.0
    known: int = 0
    hits: int = 0
    missing: int = 0

    def register(self, score: float, hit: bool) -> None:
        self.known += 1
        self.weight += score
        if hit:
            self.hits += 1
            self.hit_weight += score

    @property
    def weighted_rate_percent(self) -> float | None:
        """相似度加权的连板率，百分数；一个可用样本都没有时给 None，不假装是 0。"""
        if self.weight <= 0:
            return None
        return round(self.hit_weight / self.weight * 100.0, 2)


def prediction_weighted_score(*, shape: float, carry: float | None, band_lift: float | None) -> tuple[float, dict[str, float | None]]:
    """预测加权分：三证据加权，缺失项的权重摊给其余项（全缺时分数就等于形态分本身）。

    它**不是综合评分**。全app只有一个综合评分算式，就是 `score_model.score_of`，
    这一页那一列由 `row_scoring.score_row` 现算、不参与排序；这里只是把历史证据
    折成一个用来排名次的数，所以名字里不许再出现「综合」。
    """
    band_term = (
        round(min(float(band_lift), PREDICTION_LIFT_CAP) / PREDICTION_LIFT_CAP * 100.0, 2)
        if band_lift is not None
        else None
    )
    terms = {"shape": round(float(shape), 2), "carry": carry, "band": band_term}
    weights = {"shape": PREDICTION_WEIGHT_SHAPE, "carry": PREDICTION_WEIGHT_CARRY, "band": PREDICTION_WEIGHT_LIFT}
    present = [name for name, value in terms.items() if value is not None]
    if not present:  # pragma: no cover - shape 恒有值
        return 0.0, terms
    total = sum(weights[name] for name in present)
    score = sum(weights[name] * float(terms[name]) for name in present) / total
    return round(score, 2), terms


def _pool_date_of(session, as_of: date | None) -> date:
    days = quote_dates(session)
    if not days:
        raise LookupError("库里没有任何行情，无法生成次日预测池，请先同步行情")
    if as_of is None:
        return days[-1]
    wanted = [day for day in days if day <= as_of]
    if not wanted:
        raise LookupError(f"{as_of} 及之前没有已同步行情，预测池给不出来")
    return wanted[-1]


def _quote_next_map(days: list[date]) -> dict[date, date]:
    return {days[index]: days[index + 1] for index in range(len(days) - 1)}


def _calendar_next(session, pool_date: date) -> date | None:
    """交易日历里基准日之后的第一个交易日。

    日历可能比行情新（当天刚收盘、行情还没同步），这时预测目标日仍然要说得出，
    所以目标日先问日历；而连板证据只能用「已经有行情的日子」（`_quote_next_map`），
    两者分开是刻意的：日期归属可以提前知道，结果不能提前用。
    """
    return session.scalar(
        select(TradingDay.trade_date)
        .where(TradingDay.trade_date > pool_date, TradingDay.is_open.is_(True))
        .order_by(TradingDay.trade_date.asc())
        .limit(1)
    )


def carry_evidence(
    session,
    matches_by_code: dict[str, list[dict]],
    pool_date: date,
) -> dict[str, CarryEvidence]:
    """每只股票 Top K 匹配样本，以及这些样本涨停次日的连板情况。

    唯一的未来信息闸门在下面那行 `day < pool_date`：晚于基准日的正是要预测的那一天，
    等于基准日的这批也不能用——基准日的涨停标记已经被「当日涨停股不进候选」用过一次，
    再拿它当连板证据，同一个标记就在同一张榜里既当筛选条件又当证据，口径自己打架。
    宁可少用一天已知信息。
    """
    if not matches_by_code:
        return {}
    next_map = _quote_next_map(quote_dates(session))
    pairs = [
        (code, match)
        for code, matches in matches_by_code.items()
        for match in matches
    ]
    known_days: set[date] = set()
    for _code, match in pairs:
        limit_day = date.fromisoformat(match["limit_date"])
        nxt = next_map.get(limit_day)
        if nxt is not None and nxt < pool_date:
            known_days.add(nxt)
    outcomes = _limit_up_outcomes(session, known_days, {match["code"] for _code, match in pairs})
    evidence: dict[str, CarryEvidence] = {}
    for code, match in pairs:
        item = evidence.setdefault(code, CarryEvidence())
        limit_day = date.fromisoformat(match["limit_date"])
        nxt = next_map.get(limit_day)
        if nxt is None or nxt >= pool_date:
            item.missing += 1
            continue
        outcome = outcomes.get((match["code"], nxt))
        if outcome is None:
            item.missing += 1
            continue
        item.register(float(match["score"] or 0.0), outcome)
    return evidence


def _limit_up_outcomes(session, days: set[date], sample_codes: set[str]) -> dict[tuple[str, date], bool]:
    if not days or not sample_codes:
        return {}
    rows = session.execute(
        select(DailyQuote.stock_code, DailyQuote.trade_date, DailyQuote.is_limit_up).where(
            and_(
                DailyQuote.trade_date.in_(sorted(days)),
                DailyQuote.stock_code.in_(sorted(sample_codes)),
            )
        )
    ).all()
    return {(code, day): bool(flag) for code, day, flag in rows}


def build_pool(session, *, as_of: date | None = None, pool_size: int = DEFAULT_POOL_SIZE) -> dict:
    """生成一份次日预测池。这是纯读+现算，不写库，随时可以再生成一次。"""
    pool_date = _pool_date_of(session, as_of)
    size = max(1, min(int(pool_size), MAX_POOL_SIZE))

    scored = score_day(session, pool_date, top_matches=EVIDENCE_MATCH_LIMIT)
    items = scored["items"]
    if not items:
        raise LookupError(
            f"{pool_date} 给不出预测池："
            + (
                f"截至该日的涨停样本池是空的（跳过 {len(scored['skipped_samples'])} 条），"
                "先同步行情或把保留交易日调大"
                if not scored["pool_size"]
                else "当天可打分的候选股票是 0 只（行情凑不齐三日窗口或全被脏行挡掉）"
            )
        )
    pool = items[:size]
    pool_total = len(items)

    next_map = _quote_next_map(quote_dates(session))
    next_trade_date = _calendar_next(session, pool_date) or next_map.get(pool_date)

    codes = [item["code"] for item in pool]
    quotes = _quote_map(session, pool_date, codes)
    evidence = carry_evidence(
        session,
        {item["code"]: item["matches"] for item in pool},
        pool_date,
    )
    bands, run = band_stats(session)
    #: 综合评分那一列的原料：基准日那一根的日线特征，和这只股票自己的 AI 推荐账。
    #: 取法与盘中/AI 那条链路逐字一致（同一个 `row_scoring`），否则同一只股票
    #: 同一天会在两个页面上顶着同一个名字给出两个数。
    day_features = row_scoring.features_at(session, codes, pool_date)
    ledger = ai_picks.history_overview(session, codes)

    rows: list[dict] = []
    for index, item in enumerate(pool, start=1):
        carry = evidence.get(item["code"]) or CarryEvidence()
        stat = bands.get(item["band"])
        samples = int((stat or {}).get("sample_count") or 0)
        enough = stat is not None and samples >= MIN_BAND_OBSERVATIONS
        band_rate = (
            float(stat["limit_up_rate"])
            if enough and stat.get("limit_up_rate") is not None
            else None
        )
        band_lift = (
            round(band_rate / float(run.baseline_rate), 4)
            if band_rate is not None and run is not None and float(run.baseline_rate or 0.0) > 0
            else None
        )
        score, terms = prediction_weighted_score(
            shape=item["max_similarity"] or 0.0,
            carry=carry.weighted_rate_percent,
            band_lift=band_lift,
        )
        #: 综合评分那一列：档位发生率在回测侧是小数，`row_scoring` 只认百分数，这里换一次单位。
        #: 名次**不跟这一列排**（还是按上面那个加权分），它给的是「同一只股票在盘中榜上
        #: 那个综合评分是多少」，两处不许各算一套。
        scored_row = row_scoring.score_row(
            similarity=item["max_similarity"],
            high_count=item["high_similarity_count"],
            sample_count=item["sample_count"],
            features=day_features.get(item["code"]),
            band_rate_percent=None if band_rate is None else round(band_rate * 100.0, 6),
            band_samples=samples,
            ledger_rate_percent=(ledger.get(item["code"]) or {}).get("hit_rate_percent"),
        )
        quote = quotes.get(item["code"], {})
        _exchange, board = classify(item["code"])
        rows.append(
            {
                "stock_code": item["code"],
                "stock_name": item["name"] or quote.get("name", ""),
                "board": board or "unknown",
                "board_label": BOARD_LABELS.get(board or "unknown", "未知"),
                "is_st": bool(quote.get("is_st")),
                "pool_rank": index,
                "rank": 0,
                "max_similarity": item["max_similarity"],
                "avg_similarity": item["avg_similarity"],
                "sample_count": item["sample_count"],
                "high_similarity_count": item["high_similarity_count"],
                "high_similarity_rate": _rate(item["high_similarity_count"], item["sample_count"]),
                "band": item["band"],
                "band_rate": None if band_rate is None else round(band_rate, 6),
                "band_samples": samples,
                "band_lift": band_lift,
                "carry_known_count": carry.known,
                "carry_hit_count": carry.hits,
                "carry_missing_count": carry.missing,
                "carry_rate": carry.weighted_rate_percent,
                "close": quote.get("close"),
                "change_percent": quote.get("change_percent"),
                "volume": quote.get("volume"),
                "turnover_rate": quote.get("turnover_rate"),
                "prediction_score": score,
                "score_terms": terms,
                "indicator_scores": scored_row["indicators"],
                "formula": scored_row["formula"],
                "history_source": scored_row["history_source"],
            }
        )

    rows.sort(key=lambda row: (-row["prediction_score"], -(row["max_similarity"] or 0.0), row["stock_code"]))
    for index, row in enumerate(rows, start=1):
        row["rank"] = index

    return {
        "pool_date": pool_date.isoformat(),
        "next_trade_date": next_trade_date.isoformat() if next_trade_date else None,
        "pool_size": size,
        "pool_total": pool_total,
        "pool_dates": scored["pool_dates"],
        "sample_pool_size": scored["pool_size"],
        "skipped_samples": scored["skipped_samples"][:20],
        "missing_bars": scored["missing_bars"],
        "dirty_skipped": scored["dirty_skipped"],
        "rows": rows,
        "bands": [
            {
                "band": stat.get("band"),
                "sample_count": int(stat.get("sample_count") or 0),
                "limit_up_count": int(stat.get("limit_up_count") or 0),
                "limit_up_rate": (
                    None if stat.get("limit_up_rate") is None else float(stat["limit_up_rate"])
                ),
                "baseline_rate": (
                    None if stat.get("baseline_rate") is None else float(stat["baseline_rate"])
                ),
                "lift": None if stat.get("lift") is None else float(stat["lift"]),
                "pending_count": int(stat.get("pending_count") or 0),
            }
            for stat in bands.values()
        ],
        "baseline_rate": (
            None if run is None or run.baseline_rate is None else round(float(run.baseline_rate), 6)
        ),
        "calibrated": bool(bands) and run is not None,
        "backtest_run_id": run.id if run is not None else None,
        "backtest_day_count": int(run.day_count or 0) if run is not None else 0,
        "backtest_observation_count": int(run.observation_count or 0) if run is not None else 0,
        "min_band_observations": MIN_BAND_OBSERVATIONS,
        "note": _build_note(
            run=run,
            bands=bands,
            pool_date=pool_date,
            size=min(size, pool_total),
            pool_total=pool_total,
            next_trade_date=next_trade_date,
        ),
        "formula": prediction_formula(),
        "disclaimer": PREDICTION_DISCLAIMER_TEXT,
    }


def _rate(count: int, total: int) -> float | None:
    if not total:
        return None
    return round(int(count) / int(total), 6)


def _build_note(*, run, bands: dict[str, dict], pool_date: date, size: int, pool_total: int, next_trade_date: date | None) -> str:
    """把「这一页凭什么」写成人话，缺什么就直说缺什么。"""
    parts = [
        f"挑选池取 {pool_date} 全市场匹配度榜前 {size} 名"
        f"（当日参与打分 {pool_total} 只，当日已涨停的股票本来就不在榜里）。"
    ]
    parts.append(
        f"预测目标日为 {next_trade_date}。"
        if next_trade_date
        else f"交易日历与行情里都没有 {pool_date} 之后的交易日，等同步下一天的行情后自动补上。"
    )
    if run is None:
        parts.append(
            "还没有任何回测记录，档位历史发生率不参与打分，预测分只由匹配度与连板率合成；"
            "跑一次回测（回测页）后会自动接上。"
        )
        return "".join(parts)
    baseline = (
        f"{round(float(run.baseline_rate) * 100.0, 2)}%"
        if run.baseline_rate is not None
        else "无从谈起（一条有结果的观测都没有）"
    )
    parts.append(
        f"档位历史发生率来自第 {run.id} 次回测"
        f"（{run.start_date}~{run.end_date}，{run.day_count} 个交易日、"
        f"{run.observation_count} 条观测，全样本基准发生率 {baseline}）。"
    )
    thin = sorted(stat["band"] for stat in bands.values() if int(stat.get("sample_count") or 0) < MIN_BAND_OBSERVATIONS)
    if thin:
        parts.append(
            f"其中 {'、'.join(thin)} 档观测数不足 {MIN_BAND_OBSERVATIONS} 条，视为噪声，"
            "落在这几档的股票不按该项打分。"
        )
    return "".join(parts)


def _quote_map(session, pool_date: date, codes: list[str]) -> dict[str, dict[str, Any]]:
    """基准日的行情与股票属性，一次取满，给表格当证据列。

    只取当前数据源真的填了的列；整列为空的字段放进页面只会是一列空白。
    """
    if not codes:
        return {}
    rows = session.execute(
        select(
            DailyQuote.stock_code,
            DailyQuote.close,
            DailyQuote.change_percent,
            DailyQuote.volume,
            DailyQuote.turnover_rate,
            Stock.name,
            Stock.is_st,
        )
        .select_from(DailyQuote)
        .outerjoin(Stock, Stock.code == DailyQuote.stock_code)
        .where(DailyQuote.trade_date == pool_date, DailyQuote.stock_code.in_(codes))
    ).all()
    return {
        code: {
            "close": _num(close),
            "change_percent": _num(change),
            "volume": _num(volume),
            "turnover_rate": _num(turnover),
            "name": name or "",
            "is_st": bool(is_st),
        }
        for code, close, change, volume, turnover, name, is_st in rows
    }


def _num(value: Any) -> float | None:
    """Numeric 列在 SQLite 下回的是 Decimal，统一转 float，JSON 与前端比较都干净。"""
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


__all__ = [
    "EVIDENCE_MATCH_LIMIT",
    "PREDICTION_DISCLAIMER_TEXT",
    "CarryEvidence",
    "build_pool",
    "carry_evidence",
    "prediction_formula",
    "prediction_weighted_score",
]
