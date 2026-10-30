"""AI 前 20 榜单：取当前最相似的前 100 只，交给已配置的 AI 打 0-100 分，留前 20。

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

DIMENSIONS = (
    ("kline", "K线形态", 25),
    ("volume", "量能", 20),
    ("position", "位置与趋势", 15),
    ("gene", "涨停基因", 15),
    ("similarity", "相似度", 15),
    ("market", "市场环境", 10),
)
RISK_MAX = 20

SYSTEM_PROMPT = (
    "你是一名资深 A 股短线交易与涨停板研究员，多年复盘涨停板、连板、首板、炸板、分歧转一致、龙头股和情绪周期。"
    "现在要对一批候选股做形态复盘打分。数据只有本地日线算出的价格、成交量、换手和涨停记录，"
    "没有新闻、公告、题材和基本面，不得编造或引用任何此类信息。\n"
    "评分框架（各维度 0–100 分，按权重加权，再减风险扣分，得到总分 0–100）：\n"
    "1. K线形态（25%）：实体与上下影结构、收盘位置、连续K线组合、平台/箱体突破、MA5/10/20 多头排列与斜率。\n"
    "2. 量能（20%）：温和放量、缩量洗盘、放量突破、量比异常、换手率是否健康（过低无人气、过高筹码松动）。\n"
    "3. 位置与趋势（15%）：在近20/60日区间的位置、距近期高点的距离、整体趋势方向。\n"
    "4. 涨停基因（15%）：近30个交易日涨停次数、最近一次涨停、是否炸板、涨停后回撤幅度、连板高度。\n"
    "5. 相似度（15%）：与近30个交易日涨停样本池的综合相似度及最相似样本。\n"
    "6. 市场环境（10%）：当日涨停/跌停家数、炸板率、最高连板高度、涨跌比所反映的情绪周期位置。\n"
    f"风险扣分（0–{RISK_MAX}）：高位急拉、长上影、放量滞涨、换手极高、连续加速后乖离过大、炸板后弱势等。\n"
    "每只输出：总分 score、各维度分 sub、风险扣分 risk_deduct、理由 reason（60 字以内，点出最关键的形态与量能特征）、"
    "风险提示 risk（30 字以内）、标签 tags（1–4 个，如 缩量回调、平台突破、首板回封、放量突破、均线多头、高位滞涨、分歧换手）。\n"
    "规则：只用给出的数据；缺失字段（-）不当作 0，按已有信息判断；评分是形态复盘分，不是涨停概率，"
    "不得写成买入建议、目标价或确定性判断。\n"
    '只返回 JSON：{"items":[{"code":"600000","score":78,"sub":{"kline":80,"volume":75,"position":70,"gene":60,"similarity":85,"market":55},'
    '"risk_deduct":5,"reason":"...","risk":"...","tags":["缩量回调","均线多头"]}]}'
)


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
    from backend.app.core.config import sample_pool_days
    from backend.app.services.ai_context import load_bars, stock_metrics

    payload = scan_market(session, day, limit, 0)
    pool = sample_pool_days()
    for item in payload["items"]:
        item["board_name"] = board_name(item["code"])
        item["bars"] = _recent_bars(session, item["code"], day)
        item["metrics"] = stock_metrics(load_bars(session, item["code"], day), pool)
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
    sub_raw = entry.get("sub") or entry.get("subscores") or entry.get("dimensions") or {}
    sub: dict[str, float | None] = {}
    if isinstance(sub_raw, dict):
        for key, _label, _w in DIMENSIONS:
            value = _num(sub_raw.get(key))
            sub[key] = None if value is None else round(max(0.0, min(100.0, value)), 1)
    deduct = _num(entry.get("risk_deduct"))
    deduct = 0.0 if deduct is None else max(0.0, min(float(RISK_MAX), abs(deduct)))
    score = _num(entry.get("score"))
    if score is None:
        have = [(sub[key], w) for key, _l, w in DIMENSIONS if sub.get(key) is not None]
        if not have:
            return None
        score = sum(v * w for v, w in have) / sum(w for _v, w in have) - deduct
    tags = entry.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in tags.replace("，", ",").replace("、", ",").split(",")]
    tags = [str(t).strip()[:8] for t in tags if str(t).strip()][:4] if isinstance(tags, list) else []
    return {
        "score": round(max(0.0, min(100.0, score)), 1),
        "reason": str(entry.get("reason") or "").strip()[:90],
        "risk": str(entry.get("risk") or "").strip()[:60],
        "risk_deduct": round(deduct, 1),
        "sub": sub,
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
) -> dict:
    """day 为空时用「此刻」：交易时段用盘中快照当临时日线。指定 day 时只用那天及以前的正式日线。"""
    from backend.app.services.intraday_service import provisional_day

    if chat is None and not ai_ready():
        raise AiNotConfigured("未配置AI接口")
    if day is not None:
        if day > today():
            raise ValueError("不能用未来的日期")
        return _rank(session, day, chat, trigger, {"kind": "daily", "taken_at": None, "count": 0})
    with provisional_day(session, moment, prefer) as (as_of, info):
        if as_of is None:
            raise LookupError("还没有行情")
        return _rank(session, as_of, chat, trigger, info)


def _rank(session, day: date, chat, trigger: str, snapshot: dict) -> dict:
    pool = candidates(session, day)
    items = pool["items"]
    if not items:
        raise LookupError("这一天没有可打分的候选股票")
    from backend.app.services.ai_context import market_context, market_line

    market = market_context(session, day)
    scores = _score_batches(day, items, chat, market_line(market))
    extra = {
        "batches": {k: PROGRESS.get(k) for k in ("total", "failed", "split", "batch_size")},
        "unscored_count": sum(1 for item in items if item["code"] not in scores),
    }
    model = "" if chat is not None else str(read_settings_file().get("ai_model") or "")
    return _save_ranking(session, day, pool, market, scores, trigger, snapshot, model, extra)


def _save_ranking(session, day, pool, market, scores, trigger, snapshot, model, extra=None, names=None) -> dict:
    items = pool["items"]
    by_code = {item["code"]: item for item in items}
    ranked = []
    order = list(by_code) + [code for code in scores if code not in by_code]
    for code in order:
        got = scores.get(code)
        if got is None:
            continue
        item = by_code.get(code) or {"code": code, "name": (names or {}).get(code, ""), "score": 0.0}
        ranked.append(
            {
                "code": code,
                "name": item.get("name") or (names or {}).get(code, ""),
                "board_name": item.get("board_name") or board_name(code),
                "ai_score": got["score"],
                "reason": got["reason"],
                "risk": got.get("risk", ""),
                "risk_deduct": got.get("risk_deduct", 0),
                "sub": got.get("sub") or {},
                "tags": got.get("tags") or [],
                "similarity": item.get("score"),
                "shape_score": item.get("shape_score"),
                "volume_score": item.get("volume_score"),
                "volume_pct_score": item.get("volume_pct_score"),
                "best_match": item.get("best_match"),
                "close": (item.get("bars") or [{}])[-1].get("close"),
            }
        )
    ranked.sort(key=lambda row: (-row["ai_score"], -(row["similarity"] or 0), row["code"]))
    ranked = ranked[:KEEP]
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    run_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    payload = {
        "date": day.isoformat(),
        "run_at": run_at,
        "trigger": trigger,
        "model": model,
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
        "items": ranked,
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


def get_ranking(session, day: date | None = None) -> dict | None:
    query = session.query(AiRanking)
    if day is not None:
        query = query.filter(AiRanking.trade_date == day)
    row = query.order_by(AiRanking.trade_date.desc(), AiRanking.id.desc()).first()
    if row is None:
        return None
    payload = dict(row.payload)
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

    if not ai_ready():
        logger.info("没配 AI，跳过定时榜单")
        return None
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
