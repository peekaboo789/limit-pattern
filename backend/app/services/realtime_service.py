"""盘中实时监控。榜上的价格来自行情快照，形态分数来自已收盘的涨停样本。

和盘后扫描有三处关键差别，全部显式写在代码里，不做隐式假设：

1. **样本库只用已收盘的。** 当天盘中的涨停还没有收盘确认，随时可能开板，
   所以基准样本池取严格早于评估日的那三个交易日；榜上「已涨停」一律标未确认。
2. **已涨停和尚未涨停两榜互斥。** 把已经封板的股票混进「像涨停前」的名单，
   是这类工具最容易骗人的地方。
3. **拿不到盘中快照就照实说。** 快照取不到时改用库里最近一个交易日收盘数据回放，
   结果带 `source="settled"` 和一句原因，不冒充实时。

形态窗口最后一天用哪根 K 线也在这里讲清楚：上午用当天到此刻的半天线，
11:30 之后只用上午当场留住的那一份。半天线的量能只有半日，和全天线不在一把尺子上，
所以 `window_mode` 必须跟着榜出去，页面显示出来。
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from typing import Callable

import numpy as np
from sqlalchemy import func

from backend.app.algorithms.features import Bar, build_three_day_window
from backend.app.algorithms.similarity import band_label, score_components
from backend.app.core import clock
from backend.app.core.constants import BAND_HIGH
from backend.app.core.config import data_dir, read_settings_file
from backend.app.core.logging import get_logger
from backend.app.models.tables import CalcBar, DailyQuote, Stock
from backend.app.providers.markets import BOARD_LABELS, classify, is_in_scope
from backend.app.providers.router import _module, normalize_source, order_for
from backend.app.services.history_backfill import load_bars_bulk
from backend.app.services.intraday_capture import (
    capture_header,
    capture_morning_bars,
    load_morning_bars,
    morning_bar,
)
from backend.app.services.limit_up_service import limit_up_price, ratio_for
from backend.app.services.similarity_service import limit_up_samples
from backend.app.services.sync_service import is_source_outage

logger = get_logger("realtime")

DISCLAIMER = "相似度不是涨停概率"

MODE_AUTO = "auto"
MODE_REALTIME = "realtime"
MODE_SETTLED = "settled"

SOURCE_REALTIME = "realtime"
SOURCE_SETTLED = "settled"

WINDOW_INTRADAY_MORNING = "intraday_morning"
WINDOW_SETTLED = "settled"

#: 一次快照请求能带多少只。腾讯实测两百只一问没问题，新浪保守一些。
SPOT_CHUNK = {"tencent": 200, "sina": 100, "eastmoney": 100}
DEFAULT_CHUNK = 100

#: 现价和涨停价都只精确到一分，判定和「还差多少」都用两位小数比。
_EPS = 0.005

_SICK_LOCK = threading.Lock()
_SICK: set[str] = set()
_FETCH_SPOT: Callable[[str, list[str]], list[dict]] | None = None
_OVERRIDE = False

#: 已收盘的涨停样本池。基准日没变、库里的行情和补来的 K 线行数没变，样本就不会变，
#: 而每轮重建它要几百次查询外加补拉网络请求——两秒的快刷节奏根本放不下。
_SAMPLES_CACHE_LOCK = threading.Lock()
_SAMPLES_CACHE: tuple[tuple, tuple] | None = None


class RealtimeUnavailable(RuntimeError):
    """没有可用的盘中快照，也没有可回放的收盘行情。"""


def clear_samples_cache() -> None:
    """让下一轮重新凑样本池。同步写完行情、清理完数据之后调用。"""
    global _SAMPLES_CACHE
    with _SAMPLES_CACHE_LOCK:
        _SAMPLES_CACHE = None


def set_spot_fetcher(fetcher=None) -> None:
    """测试注入。不传就恢复真实行情源。"""
    global _FETCH_SPOT, _OVERRIDE
    if fetcher is None:
        _OVERRIDE = False
        _FETCH_SPOT = None
        _clear_sick()
    else:
        _OVERRIDE = True
        _FETCH_SPOT = fetcher
    clear_samples_cache()



def _mark_sick(name: str) -> None:
    with _SICK_LOCK:
        _SICK.add(name)


def _is_sick(name: str) -> bool:
    with _SICK_LOCK:
        return name in _SICK


def _clear_sick() -> None:
    with _SICK_LOCK:
        _SICK.clear()


def _spot_chain() -> list[str]:
    """快照按设置里的源顺序取。指定单一源时不换，auto 时依次回退。"""
    if _OVERRIDE:
        return ["mock"]
    configured = normalize_source(str(read_settings_file().get("data_source") or "auto"))
    return order_for(configured)


def fetch_spot(codes: list[str]) -> tuple[str, dict[str, dict]]:
    """取一批代码的盘中快照，返回 (第一个给出价格的源, {代码: 那一行})。

    某个源在某一块代码上失败，剩下的块交给下一个源；某个源一旦 5xx 或限流，
    这一轮不再试它——和同步那边逐只换源是同一个道理，只是这里一次问两百只。
    没有任何源给出过的代码（停牌、退市、当日没开盘）就当轮缺席，
    不留着上一轮的价格冒充实时。
    """
    wanted = sorted({code for code in codes if is_in_scope(code)})
    if not wanted:
        return "", {}
    if _OVERRIDE:
        assert _FETCH_SPOT is not None
        rows = list(_FETCH_SPOT("mock", wanted) or [])
        return "mock", {str(row["code"]): row for row in rows if row.get("code")}
    by_code: dict[str, dict] = {}
    first = ""
    last: Exception | None = None
    for name in _spot_chain():
        if _is_sick(name):
            continue
        remaining = [code for code in wanted if code not in by_code]
        if not remaining:
            break
        module = _module(name)
        chunk = SPOT_CHUNK.get(name, DEFAULT_CHUNK)
        for start in range(0, len(remaining), chunk):
            batch = remaining[start : start + chunk]
            try:
                rows = module.fetch_spot(batch) or []
            except Exception as exc:
                if is_source_outage(exc):
                    _mark_sick(name)
                logger.info("数据源 %s 取快照失败，剩下的改用下一个源：%s", name, exc)
                last = exc
                break
            for row in rows:
                code = str(row.get("code") or "")
                if code and code not in by_code:
                    row["source"] = row.get("source") or name
                    by_code[code] = row
                    if not first:
                        first = name
    if not by_code:
        raise RealtimeUnavailable(str(last) or "行情快照是空的")
    return first or chain_head(), by_code


def chain_head() -> str:
    chain = _spot_chain()
    return chain[0] if chain else ""


def _samples_of(session, base_date: date) -> tuple[list[date], list[dict], list[dict]]:
    """已收盘的涨停样本池，带缓存。

    快刷两秒一轮，而重凑一次样本要给每个涨停日补 K 线：几百次查询，凑不齐的还要发网络请求。
    指纹取基准日加两张表的行数和收盘合计——同步写行情、清理删行情都会改动其中一项，
    下一轮自然重算；什么都没动时这一轮一次样本查询都不发。
    """
    global _SAMPLES_CACHE
    rows, total = session.query(func.count(DailyQuote.id), func.sum(DailyQuote.close)).one()
    calc_rows = session.query(func.count(CalcBar.id)).scalar()
    key = (
        str(data_dir()),
        base_date,
        int(rows or 0),
        round(float(total or 0.0), 2),
        int(calc_rows or 0),
    )
    with _SAMPLES_CACHE_LOCK:
        cached = _SAMPLES_CACHE
        if cached is not None and cached[0] == key:
            return cached[1]
    pool = limit_up_samples(session, base_date)
    with _SAMPLES_CACHE_LOCK:
        _SAMPLES_CACHE = (key, pool)
    return pool


def universe(session, codes: list[str] | None) -> list[str]:
    """这一轮要给哪些股票打分。`codes=None` 表示全市场。

    盘中当天的行情还没进库，所以全市场的成员来自股票列表；列表是空的（首次同步前）
    才退回「库里最后一个交易日有行情的这些」。
    """
    if codes is not None:
        return sorted({code for code in codes if is_in_scope(code)})
    found = {row[0] for row in session.query(Stock.code) if is_in_scope(row[0])}
    if found:
        return sorted(found)
    latest = _last_quote_date(session)
    if latest is None:
        return []
    return sorted({row[0] for row in session.query(DailyQuote.stock_code).filter(DailyQuote.trade_date == latest).distinct() if is_in_scope(row[0])})


def _round(value) -> float | None:
    return None if value is None else round(float(value), 2)


def _number(spot: dict, field: str) -> float | None:
    value = spot.get(field)
    return None if value is None else float(value)


def _limit_of(code: str, pre_close: float | None, is_st: bool, day: date) -> tuple[float | None, float | None]:
    """按当天规则算 (涨停价, 适用比例百分数)。

    板块判不出来或前收缺失就两个都给 None：宁可不填这一列，也不猜一个 10%。
    """
    if pre_close is None or pre_close <= 0:
        return None, None
    _exchange, board = classify(code)
    if not board:
        return None, None
    ratio = ratio_for(board, is_st, day)
    return float(limit_up_price(pre_close, ratio)), float(ratio * 100)


def _to_limit(pre_close: float | None, latest: float | None, limit_price: float | None) -> float | None:
    """还差几个点到涨停价，分母是前收。已封板给 0.0，缺输入给 None，不给负数。

    不写成「涨停幅度 − 涨幅」：涨停价是前收乘比例再四舍五入到一分，两者不在同一分母上。
    现价高于涨停价的脏数据夹到 0.0——负号只会让人以为接口出错。
    """
    if latest is None or limit_price is None or pre_close is None or pre_close <= 0:
        return None
    if latest + _EPS >= limit_price:
        return 0.0
    return round(max(0.0, (limit_price - latest) / pre_close * 100.0), 2)


def morning_window(
    *,
    trade_date: date,
    spots: dict[str, dict],
    codes: list[str],
    full_round: bool,
    source: str,
) -> tuple[dict[str, Bar], datetime | None, str]:
    """这一轮的半天线、它是几点留的，以及拿不到时要照实说的那句话。

    只有全市场轮才写盘：这样文件里每一根线来自同一刻，下午才说得出「窗口冻结于几点」。
    快刷轮（榜前那几百只）若也覆盖写，冻结值就变成「上午最后一次只覆盖榜上这几百只」，
    下午换口径重选的股票会大面积拿不到线。
    """
    phase = clock.session_phase()
    if phase == "pre_open":
        return {}, None, "现在还没到 9:30，当天没有上午走势：形态窗口最后一天是已收盘的基准日"
    if phase == "morning":
        bars: dict[str, Bar] = {}
        for code in codes:
            spot = spots.get(code)
            if spot is None:
                continue
            bar = morning_bar(spot)
            if bar is not None:
                bars[code] = bar
        if not bars:
            return {}, None, (
                f"{source} 快照不给今开/最高/最低，造不出当天半天线："
                "形态窗口最后一天是已收盘的基准日"
            )
        if full_round:
            capture_morning_bars(trade_date, bars)
        return bars, clock.now(), ""
    captured = load_morning_bars(trade_date)
    if captured is None:
        return {}, None, (
            "11:30 之前没有跑过全市场取数，上午的半天线没被留住；下午的报价是全天累计量："
            "形态窗口最后一天是已收盘的基准日"
        )
    bars = {code: captured.bars[code] for code in codes if code in captured.bars}
    if not bars:
        return {}, None, (
            f"冻结在 {captured.captured_at:%H:%M} 的上午半天线里没有这一轮的股票："
            "形态窗口最后一天是已收盘的基准日"
        )
    return bars, captured.captured_at, ""


def _score(
    session,
    *,
    codes: list[str],
    samples: list[dict],
    as_of: date,
    bars_by_code: dict[str, list[Bar]],
    morning_bars: dict[str, Bar],
    names: dict[str, str],
) -> tuple[dict[str, dict], int]:
    """给每只股票建三日窗口并对样本池打分，返回 ({代码: 分数那一块}, 跳过的只数)。

    这一轮只要拿到了半天线，整榜就统一按「含当天上午」的窗口算：一榜里混两种窗口布局的话，
    前两名是被两把尺子量出来的，排序本身就没有意义。造不出半天线的股票当轮跳过并计数。
    """
    include_today = bool(morning_bars)
    targets: list[tuple[str, list[float]]] = []
    skipped = 0
    for code in codes:
        history = list(bars_by_code.get(code) or [])
        bar = morning_bars.get(code)
        if include_today and bar is None:
            skipped += 1
            continue
        if bar is not None:
            # 半天线代表当天那一格。库里可能已经有一根当天的全天线（白天手工同步过），
            # 这里主动丢掉，而不是交给窗口按数组顺序去重。
            history = [item for item in history if item.trade_date < bar.trade_date]
        window = build_three_day_window(
            [*history, bar] if bar is not None else history,
            as_of,
            include_as_of_date=include_today,
        )
        if not window.ok:
            skipped += 1
            continue
        targets.append((code, window.vector))
    if not targets or not samples:
        return {}, skipped
    parts = score_components(
        np.asarray([vector for _code, vector in targets], dtype=float),
        np.asarray([sample["vector"] for sample in samples], dtype=float),
    )
    out: dict[str, dict] = {}
    for row_index, (code, _vector) in enumerate(targets):
        scores = parts["score"][row_index].astype(float).copy()
        for sample_index, sample in enumerate(samples):
            if sample["code"] == code:
                # 涨停那天就在样本池里的股票不能和自己比。
                scores[sample_index] = -1.0
        if scores.size == 0 or float(scores.max()) < 0:
            continue
        best = int(np.argmax(scores))
        sample = samples[best]
        # 平均相似度和「高相似有几条」是给 AI 分析用的证据：只有最高分那一条的话，
        # 一条碰巧像和一整片都像，在模型眼里是一样的。剔掉自身那几条后再算。
        others = scores[scores >= 0.0]
        out[code] = {
            "name": names.get(code, ""),
            "score": _round(scores[best]),
            "shape_score": _round(parts["shape"][row_index, best]),
            "volume_score": _round(parts["volume"][row_index, best]),
            "volume_pct_score": _round(parts["volume_pct"][row_index, best]),
            "avg_score": None if others.size == 0 else _round(float(others.mean())),
            "high_score_count": int(np.count_nonzero(others >= BAND_HIGH)),
            "sample_count": int(others.size),
            "band": band_label(float(scores[best])),
            "best_match": {
                "code": sample["code"],
                "name": names.get(sample["code"], ""),
                "limit_date": sample["limit_date"].isoformat(),
                "score": _round(float(scores[best])),
            },
        }
    return out, skipped


def _row(
    code: str,
    scored: dict,
    prices: dict[str, float | None],
    *,
    is_st: bool,
    day: date,
    limit_price: float | None,
    at_limit: bool,
    confirmed: bool,
) -> dict:
    exchange, board = classify(code)
    latest = prices.get("latest")
    pre_close = prices.get("pre_close")
    computed, percent = _limit_of(code, pre_close, is_st, day)
    if limit_price is None:
        limit_price = computed
    return {
        "code": code,
        "name": scored["name"],
        "exchange": "SH" if exchange == "SSE" else "SZ",
        "board": board or "unknown",
        "board_label": BOARD_LABELS.get(board or "unknown", "未知"),
        "latest": _round(latest),
        "pct_chg": _round(prices.get("pct_chg")),
        "pre_close": _round(pre_close),
        "open": _round(prices.get("open")),
        "high": _round(prices.get("high")),
        "low": _round(prices.get("low")),
        "volume": prices.get("volume"),
        "amount": prices.get("amount"),
        "turnover_rate": _round(prices.get("turnover_rate")),
        "limit_percent": percent,
        "limit_up_price": _round(limit_price),
        "to_limit_percent": 0.0 if at_limit else _to_limit(pre_close, latest, limit_price),
        "is_limit_up": bool(at_limit),
        "limit_confirmed": bool(confirmed),
        "score": scored["score"],
        "shape_score": scored["shape_score"],
        "volume_score": scored["volume_score"],
        "volume_pct_score": scored["volume_pct_score"],
        "avg_score": scored["avg_score"],
        "high_score_count": scored["high_score_count"],
        "sample_count": scored["sample_count"],
        "band": scored["band"],
        "best_match": scored["best_match"],
    }


def scan(
    session,
    *,
    mode: str = MODE_AUTO,
    codes: list[str] | None = None,
    as_of_date: date | None = None,
) -> dict:
    """扫一轮。`codes=None` 是全市场，给了就是只重算那一批（榜前快刷用的就是这个）。

    `mode=settled` 直接用库里最后一个交易日的收盘数据回放，不碰行情源，
    盘后、周末和测试看到的都是这一条路。
    """
    if mode not in {MODE_AUTO, MODE_REALTIME, MODE_SETTLED}:
        raise ValueError("扫描模式只能是 auto、realtime、settled")
    fallback = ""
    spots: dict[str, dict] = {}
    source = ""
    trade_date = as_of_date

    if mode in {MODE_AUTO, MODE_REALTIME}:
        try:
            source, spots = fetch_spot(universe(session, codes))
        except RealtimeUnavailable as exc:
            if mode == MODE_REALTIME:
                raise
            fallback = str(exc) or "行情快照取不到"
            spots = {}
            source = ""
        if spots:
            days = [row.get("trade_date") for row in spots.values() if isinstance(row.get("trade_date"), date)]
            trade_date = trade_date or (max(days) if days else clock.today())

    if mode == MODE_SETTLED or (mode == MODE_AUTO and not spots):
        day = as_of_date or _last_quote_date(session)
        if day is None:
            raise RealtimeUnavailable("库里没有任何行情，无法回放")
        trade_date = day
        wanted = None if codes is None else set(codes)
        for row in session.query(DailyQuote).filter(DailyQuote.trade_date == trade_date).all():
            if not is_in_scope(row.stock_code) or (wanted is not None and row.stock_code not in wanted):
                continue
            spots[row.stock_code] = {
                "code": row.stock_code,
                "trade_date": row.trade_date,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "pre_close": row.pre_close,
                "volume": row.volume,
                "amount": row.amount,
                "turnover_rate": row.turnover_rate,
                "pct_chg": row.change_percent,
                "limit_up_price": row.limit_up_price,
                "is_limit_up": bool(row.is_limit_up),
                "source": row.source,
            }
        source = source or str(read_settings_file().get("active_source") or "local").split(",")[0]

    if trade_date is None or not spots:
        raise RealtimeUnavailable("取不到评估日或这一轮没有任何行情")

    realtime = mode != MODE_SETTLED and not fallback
    scored_codes = sorted(spots)
    base_date = _base_date(session, trade_date)
    dates, samples, skipped_samples = _samples_of(session, base_date)
    if not samples:
        raise RealtimeUnavailable(
            f"{base_date} 及之前没有可用的已收盘涨停样本，先同步行情，或把保留交易日调大一些"
        )

    bars_by_code = load_bars_bulk(session, trade_date, codes=scored_codes)
    names = {row[0]: row[1] for row in session.query(Stock.code, Stock.name)}
    st_codes = {row[0] for row in session.query(Stock.code).filter(Stock.is_st.is_(True))}
    if realtime:
        morning_bars, captured_at, window_reason = morning_window(
            trade_date=trade_date,
            spots=spots,
            codes=scored_codes,
            full_round=codes is None,
            source=source,
        )
    else:
        morning_bars, captured_at, window_reason = {}, None, "回放收盘数据，形态窗口最后一天不含当天"

    scored, skipped = _score(
        session,
        codes=scored_codes,
        samples=samples,
        as_of=trade_date,
        bars_by_code=bars_by_code,
        morning_bars=morning_bars,
        names=names,
    )

    rows: list[dict] = []
    for code, scored_block in scored.items():
        spot = spots[code]
        prices = {
            "latest": _number(spot, "close"),
            "pre_close": _number(spot, "pre_close"),
            "open": _number(spot, "open"),
            "high": _number(spot, "high"),
            "low": _number(spot, "low"),
            "volume": spot.get("volume"),
            "amount": spot.get("amount"),
            "turnover_rate": _number(spot, "turnover_rate"),
            "pct_chg": _number(spot, "pct_chg"),
        }
        if prices["pct_chg"] is None and prices["latest"] is not None and prices["pre_close"]:
            prices["pct_chg"] = (prices["latest"] / prices["pre_close"] - 1.0) * 100.0
        is_st = code in st_codes
        limit_price = _number(spot, "limit_up_price") if realtime else None
        if realtime:
            # 盘中一律按「现价对上涨停价」判，并且不承认已确认——收盘前随时可能开板。
            at_limit = bool(
                prices["latest"] is not None
                and limit_price is not None
                and prices["latest"] + _EPS >= limit_price
            )
            confirmed = False
        else:
            # 回放信库里那一天落好的涨停标签：它按当天的规则算过新股、停牌这些例外。
            at_limit = bool(spot.get("is_limit_up"))
            confirmed = True
        rows.append(
            _row(
                code,
                scored_block,
                prices,
                is_st=is_st,
                day=trade_date,
                limit_price=limit_price,
                at_limit=at_limit,
                confirmed=confirmed,
            )
        )
    rows.sort(key=lambda item: (-(item["score"] or 0.0), item["code"]))

    if realtime:
        note = (
            f"盘中快照来自 {source}，样本池是截至 {base_date} 的 {len(dates)} 个交易日涨停；"
            "「已涨停」是盘中未收盘状态，可能开板。"
        )
        if trade_date != clock.today():
            note = f"{note} 快照给的是 {trade_date} 的行情，今天不是交易日或还没有当天报价。"
    else:
        note = (
            f"回放 {trade_date} 收盘数据（不是盘中实时），样本池是截至 {base_date} 的涨停；"
            "「已涨停」是当日收盘确认结果。"
        )
    if fallback:
        note = f"{note} 切换原因：{fallback}"
    if morning_bars:
        note = (
            f"{note} 形态窗口最后一天 = {trade_date.isoformat()} 上午的半天线，"
            "量能只是半日的，和不含当天的分数不在一把尺子上。"
        )
    else:
        note = f"{note} {window_reason}"

    return {
        "trade_date": trade_date.isoformat(),
        "base_date": base_date.isoformat(),
        "pool_dates": [day.isoformat() for day in dates],
        "pool_size": len(samples),
        "generated_at": clock.now().isoformat(timespec="seconds"),
        "source": SOURCE_REALTIME if realtime else SOURCE_SETTLED,
        "provider": source,
        "phase": clock.session_phase(),
        "in_session": clock.in_session(),
        "window_mode": WINDOW_INTRADAY_MORNING if morning_bars else WINDOW_SETTLED,
        "morning_captured_at": captured_at.isoformat(timespec="seconds") if captured_at else None,
        "morning_bar_count": len(morning_bars),
        "morning_freeze": None if morning_bars else capture_header(trade_date),
        "skipped_count": skipped,
        "skipped_samples": skipped_samples[:20],
        "limit_up_count": sum(1 for row in rows if row["is_limit_up"]),
        "pending_count": sum(1 for row in rows if not row["is_limit_up"]),
        "note": note,
        "disclaimer": DISCLAIMER,
        "rows": rows,
    }


def _last_quote_date(session) -> date | None:
    row = session.query(DailyQuote.trade_date).order_by(DailyQuote.trade_date.desc()).first()
    return row[0] if row else None


def _base_date(session, trade_date: date) -> date:
    """严格早于评估日的最近一个已收盘交易日。当天盘中的涨停不能进样本池。"""
    days = [
        row[0]
        for row in session.query(DailyQuote.trade_date).filter(DailyQuote.trade_date < trade_date).distinct()
    ]
    if not days:
        raise RealtimeUnavailable(f"{trade_date} 之前没有已收盘的行情，做不了盘中扫描")
    return max(days)
