"""前 20 榜单：全市场按最高相似度取前 100，系统按固定公式算综合评分，留综合评分前 20。
AI 只当解读员：给出依据、风险、标签和一个自评分（页面显示为模型分与偏差），不决定排名。

只用已经收盘或当前时刻之前的行情，不看未来。相似度不是涨停概率。
"""

from __future__ import annotations

import json
from datetime import date, datetime

from backend.app.core.clock import SHANGHAI, today
from backend.app.core.config import rankings_dir, read_settings_file
from backend.app.core.logging import get_logger
from backend.app.models.tables import AiRanking, DailyQuote
from backend.app.services.retention import quote_dates
from backend.app.services.similarity_service import DISCLAIMER, board_name, scan_market
from backend.app.services.summary_service import AiFailed, AiNotConfigured, _extract_json, ai_ready, call_chat

logger = get_logger("ranking")

CANDIDATES = 100
KEEP = 20

from backend.app.services.score_model import ITEM_LABELS, ITEMS, PROMPT_RUBRIC

# 页面六栏：五项贡献 + 风险影响
DIMENSIONS = tuple((key, ITEM_LABELS[key], round(weight * 100)) for key, _field, weight in ITEMS)
RISK_MAX = 20

OUTPUT_FORMAT = (
    "评分是形态复盘分，不是涨停概率。每只输出：你自己的相对判断 score（0–100，只作对照，页面以系统综合评分为准）、"
    "依据 reason（60 字以内，写成模型依据）、风险提示 risk（30 字以内）、标签 tags（1–4 个）。\n"
    '只返回 JSON：{"items":[{"code":"600000","score":72,"reason":"...","risk":"...","tags":["缩量回调","均线多头"]}]}'
)
SYSTEM_PROMPT = PROMPT_RUBRIC + "\n" + OUTPUT_FORMAT


class RankingError(RuntimeError):
    pass


def _recent_bars(session, code: str, day: date, count: int = 3) -> list[dict]:
    rows = (
        session.query(DailyQuote)
        .filter(DailyQuote.stock_code == code, DailyQuote.trade_date <= day)
        .order_by(DailyQuote.trade_date.desc())
        .limit(count)
        .all()
    )
    return [
        {
            "date": row.trade_date.isoformat(),
            "close": row.close,
            "pct": row.change_percent,
            "volume": row.volume,
            "turnover": row.turnover_rate,
        }
        for row in reversed(rows)
    ]


def candidates(session, day: date, limit: int = CANDIDATES) -> dict:
    """前 100：全市场（含已涨停）按 (−最高相似度, −最高相似度, 代码) 取前 100，再算综合评分。"""
    from backend.app.core.config import sample_pool_days
    from backend.app.services.ai_context import load_bars, stock_metrics
    from backend.app.services.score_model import build_indicators, score_of

    payload = scan_market(session, day, limit, 0)
    pool = sample_pool_days()
    for item in payload["items"]:
        item["board_name"] = board_name(item["code"])
        item["bars"] = _recent_bars(session, item["code"], day)
        item["metrics"] = stock_metrics(load_bars(session, item["code"], day), pool)
        item["indicators"] = build_indicators(
            similarity=item["max_similarity"],
            high_count=item["high_similarity_count"],
            sample_count=item["sample_count"],
            features=item.get("features"),
            change_percent=item.get("change_percent"),
            history_rate_percent=item.get("history_limit_up_rate"),
        )
        item["formula"] = score_of(item["indicators"])
    return payload


BATCH_SIZE = 10  # 默认值；实际以设置 ai_batch_size / ai_parallel 为准
PARALLEL = 3
PROGRESS: dict = {"running": False, "done": 0, "total": 0, "failed": 0, "split": 0, "scored": 0, "batch_size": BATCH_SIZE}


def progress() -> dict:
    return dict(PROGRESS)


def _prompt(day: date, items: list[dict], market: str = "") -> str:
    """市场环境只写一次 + 字段说明 + 每只一行。"""
    from backend.app.services.ai_context import LEGEND, stock_line

    lines = [f"评估日 {day.isoformat()}，候选 {len(items)} 只。", market or "市场环境：-", LEGEND]
    lines.extend(stock_line(item, item.get("metrics") or {}) for item in items)
    return "\n".join(lines)


def _score_batches(day: date, items: list[dict], chat, market: str = "") -> dict[str, dict]:
    """默认每批 10 只、3 批并发；某批超时就对半拆开各重试一次；失败的批不影响其他批，已评出的结果保留。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from backend.app.core.ai_client import AiTimeout
    from backend.app.core.config import ai_int

    size = ai_int(None, "ai_batch_size")
    parallel = ai_int(None, "ai_parallel")
    batches = [items[i : i + size] for i in range(0, len(items), size)]
    lock = threading.Lock()
    scores: dict[str, dict] = {}
    errors: list[str] = []
    PROGRESS.update(running=True, done=0, total=len(batches), failed=0, split=0, scored=0, batch_size=size)

    def ask(batch: list[dict]) -> dict[str, dict]:
        prompt = _prompt(day, batch, market)
        text = chat(prompt) if chat is not None else call_chat(prompt, system=SYSTEM_PROMPT)
        return _parse_scores(text)

    def keep(result: dict[str, dict]) -> None:
        with lock:
            scores.update(result)
            PROGRESS["scored"] = len(scores)

    def fail(exc: Exception) -> None:
        with lock:
            errors.append(str(exc))
            PROGRESS["failed"] += 1

    def run(batch: list[dict]) -> None:
        try:
            keep(ask(batch))
        except AiTimeout as exc:
            if len(batch) < 2:
                fail(exc)
            else:
                with lock:
                    PROGRESS["split"] += 1
                half = (len(batch) + 1) // 2
                for part in (batch[:half], batch[half:]):  # 拆半只重试一次，不再继续拆
                    try:
                        keep(ask(part))
                    except (AiFailed, AiNotConfigured) as inner:
                        fail(inner)
        except (AiFailed, AiNotConfigured) as exc:
            fail(exc)
        finally:
            with lock:
                PROGRESS["done"] += 1

    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            list(pool.map(run, batches))
    finally:
        PROGRESS["running"] = False
    if not scores:
        raise AiFailed(errors[0] if errors else "AI 没有给出有效评分")
    return scores


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _parse_entry(entry: dict) -> dict | None:
    """模型的那一份：自评 score（只作对照）、依据、风险、标签。旧格式的 sub 各维度分加权成自评。"""
    score = _num(entry.get("score"))
    sub_raw = entry.get("sub") or entry.get("subscores") or entry.get("dimensions") or {}
    if score is None and isinstance(sub_raw, dict):
        values = [v for v in (_num(x) for x in sub_raw.values()) if v is not None]
        score = sum(values) / len(values) if values else None
    reason = str(entry.get("reason") or "").strip()[:90]
    if score is None and not reason:
        return None
    tags = entry.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in tags.replace("，", ",").replace("、", ",").split(",")]
    tags = [str(t).strip()[:8] for t in tags if str(t).strip()][:4] if isinstance(tags, list) else []
    return {
        "score": None if score is None else round(max(0.0, min(100.0, score)), 1),
        "reason": reason,
        "risk": str(entry.get("risk") or "").strip()[:60],
        "tags": tags,
    }


def _parse_scores(text: str) -> dict[str, dict]:
    payload = _extract_json(text)
    raw = payload.get("items")
    if not isinstance(raw, list):
        raise AiFailed("AI 没有返回 items 列表")
    scores: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip()[-6:]
        parsed = _parse_entry(entry)
        if code and parsed and code not in scores:
            scores[code] = parsed
    if not scores:
        raise AiFailed("AI 没有给出有效评分")
    return scores


def _latest_day(session) -> date:
    days = quote_dates(session, today())
    if not days:
        raise LookupError("还没有行情")
    return days[-1]


def generate_ranking(
    session,
    day: date | None = None,
    *,
    chat=None,
    trigger: str = "manual",
    moment: datetime | None = None,
    prefer: str = "auto",
    use_ai: bool = True,
) -> dict:
    """day 为空时用「此刻」：交易时段用盘中快照当临时日线。指定 day 时只用那天及以前的正式日线。
    没配 AI 或 AI 全部失败时照样按综合评分出榜，只是没有模型依据。"""
    from backend.app.services.intraday_service import provisional_day

    use_ai = use_ai and (chat is not None or ai_ready())
    if day is not None:
        if day > today():
            raise ValueError("不能用未来的日期")
        return _rank(session, day, chat, trigger, {"kind": "daily", "taken_at": None, "count": 0}, use_ai)
    with provisional_day(session, moment, prefer) as (as_of, info):
        if as_of is None:
            raise LookupError("还没有行情")
        return _rank(session, as_of, chat, trigger, info, use_ai)


def _rank(session, day: date, chat, trigger: str, snapshot: dict, use_ai: bool = True) -> dict:
    from backend.app.services.similarity_service import feed_order

    pool = candidates(session, day)
    items = pool["items"]
    if not items:
        raise LookupError("这一天没有可打分的候选股票")
    from backend.app.services.ai_context import market_context, market_line

    market = market_context(session, day)
    scores: dict[str, dict] = {}
    ai_error = "" if use_ai else "没有配置 AI 接口：只按系统综合评分出榜，没有模型依据"
    if use_ai:
        try:
            scores = _score_batches(day, feed_order(items), chat, market_line(market))
        except (AiFailed, AiNotConfigured) as exc:
            ai_error = f"AI 解读失败（{exc}）：只按系统综合评分出榜"
    extra = {
        "batches": {k: PROGRESS.get(k) for k in ("total", "failed", "split", "batch_size")} if use_ai else None,
        "unscored_count": sum(1 for item in items if item["code"] not in scores) if use_ai else 0,
        "ai_error": ai_error,
    }
    model = "" if chat is not None or not use_ai else str(read_settings_file().get("ai_model") or "")
    return _save_ranking(session, day, pool, market, scores, trigger, snapshot, model, extra, select="composite")


def outside_items(session, day: date, codes: list[str], names: dict | None = None) -> list[dict]:
    """外部导入里不在前 100 的代码：也按同一公式算综合评分（扫全市场一次，不拼详细分组）。"""
    if not codes:
        return []
    from backend.app.services.score_model import build_indicators, score_of

    wanted = set(codes)
    scan = scan_market(session, day, 100000, 0, details=False)
    found = {item["code"]: item for item in scan["items"] if item["code"] in wanted}
    out = []
    for code in codes:
        item = found.get(code) or {"code": code, "name": (names or {}).get(code, ""), "max_similarity": None,
                                   "high_similarity_count": 0, "sample_count": 0}
        item["board_name"] = board_name(code)
        item["indicators"] = build_indicators(
            similarity=item.get("max_similarity"),
            high_count=item.get("high_similarity_count"),
            sample_count=item.get("sample_count"),
            features=item.get("features"),
            change_percent=item.get("change_percent"),
            history_rate_percent=item.get("history_limit_up_rate"),
        )
        item["formula"] = score_of(item["indicators"])
        out.append(item)
    return out


def pick_history(session, day: date, codes: list[str]) -> dict[str, dict]:
    """单股历史总分：该股在评估日之前上过的每日最终榜，自评均分 × (0.3 + 0.7·(h+1)/(settled+2))。
    h = 次日涨停次数，settled = 已定判（次日行情已入库）的次数，pending 不进分母。"""
    from backend.app.services.score_model import HISTORY_FORMULA, ALL_PENDING_FORMULA, hit_rate, score_from_history

    stored = quote_dates(session)
    rows = session.query(AiRanking).filter(AiRanking.trade_date < day).order_by(AiRanking.trade_date, AiRanking.id).all()
    last: dict[date, dict] = {}
    for row in rows:
        last[row.trade_date] = row.payload
    wanted = set(codes)
    picks: dict[str, list[tuple[float, date]]] = {}
    for pick_day, payload in last.items():
        for item in payload.get("items") or []:
            if item.get("code") in wanted:
                value = item.get("model_score")
                if value is None:
                    value = item.get("ai_score")
                if isinstance(value, (int, float)):
                    picks.setdefault(item["code"], []).append((float(value), pick_day))
    nexts = {}
    for _code, entries in picks.items():
        for _v, d in entries:
            later = [x for x in stored if x > d and x <= day]
            nexts[d] = later[0] if later else None
    limit_ups = {
        (c, d)
        for c, d in session.query(DailyQuote.stock_code, DailyQuote.trade_date).filter(
            DailyQuote.trade_date.in_([d for d in nexts.values() if d]), DailyQuote.is_limit_up.is_(True)
        )
    } if nexts else set()
    out = {}
    for code in codes:
        entries = picks.get(code) or []
        hit = sum(1 for _v, d in entries if nexts.get(d) and (code, nexts[d]) in limit_ups)
        settled = sum(1 for _v, d in entries if nexts.get(d))
        avg = round(sum(v for v, _d in entries) / len(entries), 2) if entries else None
        out[code] = {
            "picks": len(entries),
            "hit": hit,
            "miss": settled - hit,
            "pending": len(entries) - settled,
            "avg_score": avg,
            "hit_rate": hit_rate(hit, settled - hit),
            "total": score_from_history(avg, hit, settled),
            "formula": HISTORY_FORMULA if settled else ALL_PENDING_FORMULA,
        }
    return out


def _save_ranking(session, day, pool, market, scores, trigger, snapshot, model, extra=None, names=None, select="composite", outside=None) -> dict:
    """选前 20：select="composite" 时从全部候选里按系统综合评分取；select="scores" 时只取 scores 里的代码（外部导入）。"""
    from backend.app.services.score_model import read_composite, read_level, with_gap

    items = pool["items"]
    by_code = {item["code"]: item for item in items}
    for item in outside or []:
        by_code.setdefault(item["code"], item)
    if select != "composite":
        for code in scores:  # 外部导入：一个都不丢，找不到候选数据就只留代码，综合分显示 —
            by_code.setdefault(code, {"code": code, "name": (names or {}).get(code, ""), "max_similarity": None, "formula": {}})
    codes = list(by_code) if select == "composite" else list(scores)
    ranked = []
    for code in codes:
        item = by_code[code]
        got = scores.get(code) or {}
        formula = with_gap(item.get("formula") or {}, got.get("score"))
        composite = read_composite(formula)
        ranked.append(
            {
                "code": code,
                "name": item.get("name") or (names or {}).get(code, ""),
                "board_name": item.get("board_name") or board_name(code),
                "composite_score": composite,
                "level": read_level(formula),
                "contributions": formula.get("contributions") or {},
                "risk_penalty": formula.get("risk_penalty"),
                "formula": formula,
                "ai_score": composite,  # 兼容旧字段：监控页显示的是系统综合评分
                "model_score": got.get("score"),
                "model_gap": formula.get("model_gap"),
                "reason": got.get("reason", ""),
                "risk": got.get("risk", ""),
                "tags": got.get("tags") or [],
                "similarity": item.get("max_similarity"),
                "max_similarity": item.get("max_similarity"),
                "avg_similarity": item.get("avg_similarity"),
                "sample_count": item.get("sample_count"),
                "high_similarity_count": item.get("high_similarity_count"),
                "band": item.get("band"),
                "is_limit_up": item.get("is_limit_up"),
                "carry_rate": item.get("carry_rate"),
                "group_scores": item.get("group_scores"),
                "best_match": item.get("best_match"),
                "top_matches": item.get("top_matches"),
                "close": (item.get("bars") or [{}])[-1].get("close") if item.get("bars") else None,
            }
        )
    if select == "composite":
        ranked.sort(key=lambda row: (-(row["composite_score"] if row["composite_score"] is not None else -1), -(row["max_similarity"] or 0), row["code"]))
        ranked = ranked[:KEEP]
    else:
        # 外部导入：按外部 AI 的分数排，同分保持它给的顺序；全部保留，不按系统综合分重排
        given = {code: index for index, code in enumerate(scores)}
        ranked.sort(key=lambda row: (-(row["model_score"] if row["model_score"] is not None else -1e9), given[row["code"]]))
        if len(ranked) > KEEP:  # 只留外部分最高的前 20，多出来的写进 rejected 说明
            extra = dict(extra or {})
            extra["rejected"] = list(extra.get("rejected") or []) + [
                {"code": row["code"], "reason": f"超过 {KEEP} 只，按外部 AI 分排在第 {i} 位，未进榜"} for i, row in enumerate(ranked[KEEP:], start=KEEP + 1)
            ]
            ranked = ranked[:KEEP]
    for row in ranked:
        row["composite_rank"] = None
    for index, row in enumerate(sorted([r for r in ranked if r["composite_score"] is not None], key=lambda r: -r["composite_score"]), start=1):
        row["composite_rank"] = index
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    from backend.app.services.ranking_checks import check_ranked

    check_ranked(session, day, ranked)
    history = pick_history(session, day, [row["code"] for row in ranked])
    try:
        from backend.app.services.backtest_service import band_lifts, prediction_for

        lifts, _bt = band_lifts(session)
    except Exception as exc:  # 回测失败不影响出榜
        logger.warning("档位倍率没算出来：%s", exc)
        lifts = {}
    for row in ranked:
        row["pick_history"] = history.get(row["code"])
        row.update(prediction_for(row, lifts))
    run_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    from backend.app import __version__
    from backend.app.core.config import similarity_weights
    from backend.app.services.score_model import FORMULA_TEXT

    payload = {
        "date": day.isoformat(),
        "run_at": run_at,
        "trigger": trigger,
        "model": model,
        "app_version": __version__,
        "formula_version": 2,
        "formula_text": FORMULA_TEXT,
        "similarity_weights": list(similarity_weights()),
        "candidate_count": len(items),
        "snapshot": snapshot,
        "pool_dates": pool["pool_dates"],
        "pool_size": pool["pool_size"],
        "pool_days": len(pool["pool_dates"]),
        **(extra or {}),
        "skipped_count": len(pool.get("skipped") or []),
        "limitup_count": pool["pool_size"] + len(pool.get("skipped") or []),
        "disclaimer": DISCLAIMER,
        "market": market,
        "dimensions": [{"key": k, "label": l, "weight": w} for k, l, w in DIMENSIONS],
        "order": "composite" if select == "composite" else "external",
        "items": ranked,
        "check_count": sum(1 for row in ranked if row.get("checks")),
    }
    session.add(AiRanking(trade_date=day, run_at=run_at, trigger=trigger, payload=payload))
    session.commit()
    _write_file(session, day)
    return payload


def _runs(session, day: date) -> list[AiRanking]:
    return session.query(AiRanking).filter(AiRanking.trade_date == day).order_by(AiRanking.id).all()


def _write_file(session, day: date) -> None:
    runs = [row.payload for row in _runs(session, day)]
    if not runs:
        return
    document = {"date": day.isoformat(), "latest": runs[-1], "runs": runs}
    path = rankings_dir() / f"{day.isoformat()}.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def display_order(payload: dict) -> dict:
    """榜单的展示顺序：外部导入按外部 AI 分（同分保持存储顺序），系统生成保持原顺序。
    兼容 0.4.5 之前导入、按综合分存储的外部榜。"""
    if not payload or payload.get("trigger") != "external" or payload.get("order") == "external":
        return payload
    items = sorted(enumerate(payload.get("items") or []), key=lambda pair: (-(pair[1].get("model_score") if pair[1].get("model_score") is not None else -1e9), pair[0]))
    out = dict(payload)
    out["items"] = [dict(item, rank=index) for index, (_i, item) in enumerate(items, start=1)]
    out["order"] = "external"
    return out


def get_ranking(session, day: date | None = None) -> dict | None:
    query = session.query(AiRanking)
    if day is not None:
        query = query.filter(AiRanking.trade_date == day)
    row = query.order_by(AiRanking.trade_date.desc(), AiRanking.id.desc()).first()
    if row is None:
        return None
    payload = display_order(dict(row.payload))
    payload["runs"] = [item.run_at for item in _runs(session, row.trade_date)]
    return payload


def ranking_dates(session) -> list[str]:
    rows = session.query(AiRanking.trade_date).distinct().all()
    return sorted({row[0].isoformat() for row in rows}, reverse=True)


def previous_ranking(session, moment_day: date | None = None) -> dict | None:
    """监控用：按交易日历取上一交易日的最终榜单（当天最后一次运行）。
    上一交易日没有榜单时，退回到更早最近的一份，并在 payload 里标 stale。"""
    from backend.app.core.calendar import previous_trading_day

    moment_day = moment_day or today()
    target = previous_trading_day(moment_day)
    exact = (
        session.query(AiRanking)
        .filter(AiRanking.trade_date == target)
        .order_by(AiRanking.id.desc())
        .first()
    )
    if exact is not None:
        return dict(exact.payload)
    row = (
        session.query(AiRanking)
        .filter(AiRanking.trade_date < target)
        .order_by(AiRanking.trade_date.desc(), AiRanking.id.desc())
        .first()
    )
    if row is None:
        return None
    payload = dict(row.payload)
    payload["stale"] = True
    payload["expected_date"] = target.isoformat()
    return payload


def scheduled_ranking(trigger: str = "schedule") -> dict | None:
    from backend.app.core.database import session_scope

    from backend.app.core.calendar import is_trading_day

    if not is_trading_day(today()):
        logger.info("今天休市，跳过定时榜单")
        return None
    try:
        with session_scope() as session:
            return generate_ranking(session, trigger=trigger)
    except (AiFailed, AiNotConfigured, LookupError, ValueError) as exc:
        logger.warning("定时榜单没有生成：%s", exc)
        return None
