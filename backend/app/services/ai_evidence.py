"""给大模型看的证据行。原则只有一条：写进字段里的每个数字都能在库里追到出处。

老工程里有一项证据在这里被砍掉了，因为这一版程序没有能让它有数的原料，
留个永远为 null 的字段等于教模型猜：

* 本地证据分（三项加权的那个合成分，本来也不给模型看）

「相似度档位的次日发生率」在档2 接回来了：它吃 `backtest_service` 落下的
那份分档账，与盘中榜的概率列、预测页的档位项是同一条记录。没有回测时这一项是 null，
评分模型的历史成功率那一项退回吃 `ai_pick_results` 的对账账本，并在 `history_source`
里说清这一行到底吃的是哪一个——两套出处不混成一个数。

「匹配样本的连板延续率」在档3 接回来了，而且**不重算第二遍**：吃的就是
`prediction_service.carry_evidence`——次日预测页那一列的同一个函数、同一个基准日，
两处各算一遍迟早会漂。它的原料是 `pattern_matches` 里落库的那批匹配明细
（`match_service.matches_by_code`），所以这一项的有无跟着「这一天跑没跑过全市场扫描」：
没跑过时整项是 null，页面上那两页同时都不给这个数。

`ai_pick_results` 那份账确实也是「过去发生的频率」，只是分母是它自己推荐过的那些行，
不是全市场，所以它只当退路。`_FIELD_DOC` 里把两个数各自的出处都写清了。
"""

from __future__ import annotations

from datetime import date

from backend.app.algorithms.features import compute_day_features
from backend.app.core.constants import EVIDENCE_ROW_LIMIT, MIN_BAND_OBSERVATIONS, TEXT_LIMIT
from backend.app.services import ai_picks
from backend.app.services import match_service
from backend.app.services import prediction_service
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.score_model import (
    FORMULA_TEXT,
    PROMPT_RUBRIC,
    ROLE_PREAMBLE,
    build_indicators,
    formula_for,
    with_gap,
)

NO_INVENTION_RULE = (
    "这些数字之外的具体事实——别的股票怎么走、当天的新闻公告题材消息——输入里没有，"
    "就不要写成理由或反证；你的经验和判断可以用，凭空的事实不能用。"
)

AI_DISCLAIMER = (
    "这一栏的综合评分、等级与各项贡献都由本系统按固定公式算出（口径见下面那一行）："
    "用的每一个数字都能在库里追到出处，同样的输入永远得到同样的分。"
    "理由、反证与「它的判断标准」三段出自外部大模型：那部分追不到出处，也没有经过回测校准。"
    "它自报的相对分不参与上面那个算式，页面上与公式分并排显示、原样给出两者的差："
    "不取平均、不替任何一方圆场；它没给分时那一格是空白而不是 0 分。"
    "综合评分与等级都不是涨停概率，也不是收益预测，更不构成对某只股票明天一定涨停的判断。"
)

#: 每只股票最多摊给模型几行证据。固定榜会传更大的数进来。
DEFAULT_ROW_LIMIT = EVIDENCE_ROW_LIMIT

_FIELD_DOC = (
    "每一行是一只股票的证据，字段含义如下："
    "stock_code 六位代码；stock_name 名称；board 板块；"
    "at_limit 此刻是否封在涨停价上；limit_confirmed 这个涨停有没有收盘确认（盘中一律没有）；"
    "last_price 最新价（元）；change_percent 当日涨跌幅（百分数）；"
    "to_limit_percent 距当日涨停价还差几个点（分母是前收，已封板为 0，缺输入为 null）；"
    "max_similarity 与最近三个交易日已收盘涨停样本池的最高相似度（0~100）；"
    "avg_similarity 与同一个样本池的平均相似度；high_sim_count 相似度达到 90 以上的样本条数；"
    "sample_count 这一轮实际参与比对的样本条数（已剔掉它自己）；"
    "band 按最高相似度落的那一档；best_match 最像的那一条涨停样本（代码、名称、涨停日、分数）；"
    "similarity_band 最高相似度落的那个数值区间；"
    "band_next_day_rate_percent 同一相似度档在最近一次**已完成回测**里的次日实际涨停发生率（百分数），"
    f"band_next_day_samples 那一档的观测条数——不足 {MIN_BAND_OBSERVATIONS} 条时这两个数都不给，因为那一档还是噪声；"
    "carry_match_count 这一天的库里为它存了几条最相似涨停样本；"
    "carry_next_day_rate_percent 这些样本**涨停次日又涨停**的相似度加权比率（百分数），与次日预测页那一列同一个函数算的；"
    "carry_known 其中能追到次日行情的条数，carry_missing 追不到的条数（次日晚于基准日或那天没行情）；"
    "这一组数只有在今天跑过全市场匹配扫描并且明细落了库时才有值，没跑过时整项为 null；"
    "ai_history 它过去被本系统大模型推荐后的对账账本（推过几次、定判几次、命中率、历史总分），"
    "从没被推荐过时整项为 null；"
    "indicator_scores 评分模型那七个输入指标的原值；formula 本系统按公式算好的综合评分那份账，"
    "其中 history_source 说明历史成功率那一项这一行吃的是哪一个出处："
    "band_rate 是上面那个档位发生率，ai_pick_ledger 是 ai_history 的命中率（还没有回测时的退路）；"
    "feed_rank 这一行进提示词的位次。"
    "任何字段值为 null 都表示本系统没有这一项证据——null 不是 0，也不是「很低」，"
    "它是「这一项无从谈起」；请把缺失本身当作这一行的不确定性，而不是当作一个低值。"
    "indicator_scores 与 formula 都是本系统算完的结果，不是让你照着某一项去打分的清单："
    "分已经算好了，你不用也不能重算。其余字段（相似度、样本条数、历史账）是这些分的出处，"
    "写依据时可以引用它们。"
)

SYSTEM_PARTS = (ROLE_PREAMBLE, _FIELD_DOC, PROMPT_RUBRIC, NO_INVENTION_RULE)

INDICATOR_LABELS = {
    "similarity_score": "三日形态相似度",
    "pattern_match_rate": "近三日高相似占比",
    "technical_score": "技术形态评分",
    "volume_score": "成交量评分",
    "capital_score": "资金评分",
    "history_limit_up_rate": "历史成功率",
    "risk_score": "风险评分",
}


def system_prompt() -> str:
    """四条链路共用的那一段。数字全部来自 `score_model` 的常量，页面与提示词读同一份。"""
    return "".join(SYSTEM_PARTS)


def _round(value) -> float | None:
    return None if value is None else round(float(value), 2)


def _text(value) -> str:
    return str(value or "").strip()[:TEXT_LIMIT]


def _as_day(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def features_at(session, codes: list[str], day: date | None) -> dict[str, dict]:
    """基准日那一根的日线特征。基准日没有行情（停牌）就整只不给，不拿更早那根冒充。"""
    if day is None or not codes:
        return {}
    grouped = load_bars_bulk(session, day, codes=codes)
    found: dict[str, dict] = {}
    for code, bars in grouped.items():
        if not bars or bars[-1].trade_date != day:
            continue
        found[code] = compute_day_features(bars)
    return found


def carry_overview(session, codes: list[str], base_day: date | None) -> tuple[dict[str, dict], dict]:
    """每只股票库里 Top 匹配样本的「次日又涨停」比率。

    这里不自己数：算式在 `prediction_service.carry_evidence` 里，次日预测页那一列吃的
    是同一个函数与同一个基准日。两处各写一遍迟早会漂出两个连板率，
    而模型看到的那个数字必须能在预测页上找到同一个值。

    原料是 `pattern_matches`（`match_service.matches_by_code`），**读库不现算**：
    盘后再敲一次现算，样本池可能已经多了一天，模型引用的就成了当天看不到的数。
    基准日那天没跑过全市场扫描时这里什么都没有，返回空表并说清是哪一种空。
    """
    picked = [code for code in codes if code]
    if base_day is None or not picked:
        return {}, {"source": "no_base_date", "targets": 0, "rows": 0}
    matches = match_service.matches_by_code(session, base_day, picked)
    if not matches:
        return {}, {"source": "no_stored_matches", "targets": 0, "rows": 0}
    evidence = prediction_service.carry_evidence(session, matches, base_day)
    rows: dict[str, dict] = {}
    for code, item in evidence.items():
        rows[code] = {
            "carry_match_count": len(matches.get(code) or []),
            "carry_next_day_rate_percent": item.weighted_rate_percent,
            "carry_known": item.known,
            "carry_missing": item.missing,
        }
    return rows, {
        "source": "pattern_matches",
        "targets": len(rows),
        "rows": sum(len(value) for value in matches.values()),
        "base_date": base_day.isoformat(),
    }


def build_evidence(session, board: dict, *, row_limit: int = DEFAULT_ROW_LIMIT) -> dict:
    """把一份榜折成给模型看的证据行。

    `board` 是实时监控那份快照（含 `limit_up_rows` / `pending_rows`）。
    取不到最高相似度的行直接跳过：那一行连自己的分都说不清，进提示词只会让模型编。
    """
    rows = [*(board.get("limit_up_rows") or []), *(board.get("pending_rows") or [])]
    base_day = _as_day(board.get("base_date"))
    codes = [str(row.get("code") or "") for row in rows if row.get("code")]
    features = features_at(session, codes, base_day)
    history = ai_picks.history_overview(session, codes)
    carry, carry_meta = carry_overview(session, codes, base_day)
    #: 概率列那份底表跟着榜一起进来：整列留空的原因只写在这一处，页面和提示词读同一句。
    probability = board.get("probability") or {}
    built: list[dict] = []
    missing_similarity = 0
    for row in rows:
        code = str(row.get("code") or "")
        max_similarity = row.get("score")
        if max_similarity is None:
            missing_similarity += 1
            continue
        item_history = history.get(code)
        #: 连板那一组数缺失时保持 null：把「这一天没落库明细」写成 0% 连板率，
        #: 模型会读成「它的样本第二天都不跟」，那是凭空造出来的反证。
        item_carry = carry.get(code) or {}
        #: 历史成功率那一项的出处：有回测档位发生率就用它（和榜上那一列同一个数），
        #: 没有就退回这只股票自己的 AI 推荐账。两套数不能混着给，所以把用了哪个也落进账里。
        band_samples = int(row.get("next_day_rate_samples") or 0)
        band_rate = row.get("next_day_rate_percent")
        if band_rate is None or band_samples < MIN_BAND_OBSERVATIONS:
            band_rate = None
        history_rate = band_rate
        history_source = "band_rate" if history_rate is not None else None
        if history_rate is None and item_history:
            history_rate = item_history.get("hit_rate_percent")
            history_source = "ai_pick_ledger" if history_rate is not None else None
        indicators = build_indicators(
            similarity=max_similarity,
            high_count=row.get("high_score_count"),
            sample_count=row.get("sample_count"),
            features=features.get(code),
            history_rate_percent=history_rate,
        )
        formula = formula_for(indicators)
        formula["history_source"] = history_source
        built.append(
            {
                "stock_code": code,
                "stock_name": _text(row.get("name")),
                "board": _text(row.get("board_label") or row.get("board")),
                "at_limit": bool(row.get("is_limit_up")),
                "limit_confirmed": bool(row.get("limit_confirmed")),
                "last_price": _round(row.get("latest")),
                "change_percent": _round(row.get("pct_chg")),
                "to_limit_percent": row.get("to_limit_percent"),
                "limit_up_price": _round(row.get("limit_up_price")),
                "max_similarity": _round(max_similarity),
                "avg_similarity": _round(row.get("avg_score")),
                "high_sim_count": row.get("high_score_count"),
                "sample_count": row.get("sample_count"),
                "band": _text(row.get("band")),
                "similarity_band": _text(row.get("similarity_band")),
                "band_next_day_rate_percent": band_rate,
                "band_next_day_samples": band_samples or None,
                "best_match": row.get("best_match"),
                "carry_match_count": item_carry.get("carry_match_count"),
                "carry_next_day_rate_percent": item_carry.get("carry_next_day_rate_percent"),
                "carry_known": item_carry.get("carry_known"),
                "carry_missing": item_carry.get("carry_missing"),
                "ai_history": item_history,
                "indicator_scores": indicators,
                "formula": formula,
                "feed_rank": 0,
            }
        )
    kept = built[: max(1, int(row_limit))]
    for index, item in enumerate(kept):
        item["feed_rank"] = index + 1
    return {
        "rows": kept,
        "board_rows": len(rows),
        "feed_rows": len(kept),
        "skipped_rows": missing_similarity,
        "history_missing_rows": sum(1 for item in kept if not item["ai_history"]),
        "feature_missing_rows": sum(1 for item in kept if item["indicator_scores"]["technical_score"] is None),
        "trade_date": str(board.get("trade_date") or ""),
        "base_date": str(board.get("base_date") or ""),
        "window_mode": str(board.get("window_mode") or ""),
        "metric": str(board.get("metric") or ""),
        "metric_label": _text(board.get("metric_label")),
        "board_generated_at": str(board.get("generated_at") or ""),
        "pool_size": int(board.get("pool_size") or 0),
        "source": str(board.get("source") or ""),
        "probability_run_id": probability.get("run_id"),
        "probability_note": str(probability.get("note") or ""),
        "band_rate_rows": sum(1 for item in kept if item["band_next_day_rate_percent"] is not None),
        "carry_rows": sum(1 for item in kept if item["carry_next_day_rate_percent"] is not None),
        "carry_source": carry_meta.get("source", ""),
        "carry_meta": carry_meta,
        #: 市场级的那一个数：当天落库汇总里高相似（阈值见 `high_threshold`）样本占比。
        #: 它不进每一行（每行都重复同一个数只会让模型以为是个股指标），
        #: 由 `ai_analysis_service` 拼在提示词抬头那一句里，出处是 `match_summaries`。
        "market_high_similarity": (
            match_service.high_similarity_share(session, base_day)
            if base_day is not None
            else {"targets": 0, "high_rate_percent": None, "source": "no_base_date"}
        ),
    }


def prompt_row_of(row: dict) -> dict:
    """模型真正看到的那一行。历史账只留能对账的那几个数，`recent` 那些原文不进提示词。"""
    payload = {key: value for key, value in row.items() if key != "ai_history"}
    history = row.get("ai_history")
    if isinstance(history, dict):
        payload["ai_history"] = {
            key: history.get(key)
            for key in ("picks", "hits", "misses", "pending", "settled", "hit_rate_percent", "avg_score", "history_score")
        }
    return payload


def evidence_line(row: dict) -> str:
    """页面上一行紧凑证据。缺失一律留空而不是 0。"""
    formula = row.get("formula") or {}
    parts = [
        f"最高相似 {row.get('max_similarity') if row.get('max_similarity') is not None else '—'}",
        f"平均 {row.get('avg_similarity') if row.get('avg_similarity') is not None else '—'}",
        f"高相似 {row.get('high_sim_count') if row.get('high_sim_count') is not None else '—'}"
        f"/{row.get('sample_count') if row.get('sample_count') is not None else '—'}",
        f"距涨停 {row.get('to_limit_percent') if row.get('to_limit_percent') is not None else '—'}%",
        f"档内次日 {row.get('band_next_day_rate_percent') if row.get('band_next_day_rate_percent') is not None else '—'}%",
        f"样本连板 {row.get('carry_next_day_rate_percent') if row.get('carry_next_day_rate_percent') is not None else '—'}%",
        f"综合 {formula.get('composite_score') if formula.get('has_signal') else '—'}",
    ]
    history = row.get("ai_history")
    if isinstance(history, dict):
        parts.append(f"AI 老账 推 {history.get('picks')} 次·定判 {history.get('settled')} 次")
    else:
        parts.append("AI 老账 没推荐过")
    return " · ".join(parts)


__all__ = [
    "AI_DISCLAIMER",
    "FORMULA_TEXT",
    "INDICATOR_LABELS",
    "NO_INVENTION_RULE",
    "ROLE_PREAMBLE",
    "build_evidence",
    "carry_overview",
    "evidence_line",
    "features_at",
    "prompt_row_of",
    "system_prompt",
    "with_gap",
]
