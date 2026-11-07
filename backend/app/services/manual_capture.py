"""手动采集当前行情并选前 100（v0.4.2）。

一键：拉全市场实时报价（新浪优先，沿用行情源顺序与熔断）→ 按已过交易时间把累计量换算成全天估算 →
当作「截至 HH:MM」的临时日线插入 → 和样本池做 67 维相似度 → 前 100（附综合评分、预测分）。
临时日线算完即删；原始报价和结果存在 data/snapshots/manual/，同一天只留最新一份（另记当天采集时间列表）。
当天正式日线同步之后，快照标记为「已被正式日线取代」，不再参与任何计算。
"""

from __future__ import annotations

import json
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time

from backend.app.core.calendar import is_trading_day, previous_trading_day
from backend.app.core.clock import SHANGHAI, now
from backend.app.core.config import data_dir
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyQuote
from backend.app.services import intraday_service as intraday
from backend.app.services.retention import quote_dates

logger = get_logger("manual_capture")

MANUAL_KIND = "intraday"  # 临时日线来源标记：盘中 intraday / 收盘后 close
STATE: dict = {"running": False, "phase": "idle", "done": 0, "total": 0, "message": "", "error": "", "started_at": None,
               "result_date": None, "taken_at": None, "elapsed_seconds": None}
_CANCEL = threading.Event()
_LOCK = threading.Lock()


class NoDataYet(LookupError):
    """开盘前或休市日：今天还没有行情。"""


def manual_dir():
    path = data_dir() / "snapshots" / "manual"
    path.mkdir(parents=True, exist_ok=True)
    return path


def status() -> dict:
    with _LOCK:
        return dict(STATE)


def cancel() -> dict:
    _CANCEL.set()
    with _LOCK:
        if STATE["running"]:
            STATE["message"] = "正在取消…"
    return status()


def _set(**values) -> None:
    with _LOCK:
        STATE.update(values)


def classify(moment: datetime) -> dict:
    """此刻能采什么：intraday（含午休，已过分钟按 2 小时算）/ close（收盘后整天）/ none（开盘前或休市）。"""
    moment = moment.astimezone(SHANGHAI)
    day = moment.date()
    if not is_trading_day(day):
        return {"mode": "none", "day": day, "reason": "今天休市，没有今天的行情", "fallback": previous_trading_day(day)}
    clock = moment.time()
    if clock < time(9, 30):
        return {"mode": "none", "day": day, "reason": "还没开盘（9:30 开始连续竞价），今天还没有行情", "fallback": previous_trading_day(day)}
    if clock >= time(15, 0):
        return {"mode": "close", "day": day, "elapsed": float(intraday.SESSION_MINUTES), "fraction": 1.0}
    elapsed = intraday.elapsed_minutes(moment)
    return {"mode": "intraday", "day": day, "elapsed": elapsed, "fraction": intraday.volume_fraction(elapsed)}


def normalize(rows: list[dict], fraction: float) -> list[dict]:
    """先用流通股本补换手率（按累计量算），再把量 / 额 / 换手同比例换算成全天估算值。"""
    try:
        from backend.app.providers import float_shares

        if intraday._bulk_fetcher is None:  # 测试注入行情时不联网
            float_shares.prefetch([str(r["code"]) for r in rows])
        for row in rows:
            float_shares.fill_turnover(str(row["code"]), [row])
    except Exception as exc:  # 补不到就缺着，相似度里按中位数补
        logger.info("流通股本没拿到：%s", exc)
    return [intraday.scale_partial(row, fraction) for row in rows]


def _fetch(codes: list[str]) -> list[dict]:
    if intraday._bulk_fetcher is not None:
        _set(done=len(codes), total=len(codes))
        return intraday._bulk_fetcher(codes)
    batches = [codes[i : i + intraday.BATCH] for i in range(0, len(codes), intraday.BATCH)]
    rows: list[dict] = []
    _set(total=len(codes), done=0)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(intraday._fetch_batch, batch) for batch in batches]
        for future, batch in zip(futures, batches):
            if _CANCEL.is_set():
                for f in futures:
                    f.cancel()
                raise InterruptedError("已取消")
            rows.extend(future.result())
            with _LOCK:
                STATE["done"] += len(batch)
    return rows


def _paths(day: date):
    base = manual_dir()
    return base / f"{day.isoformat()}.json", base / f"{day.isoformat()}-index.json"


def save_snapshot(day: date, payload: dict) -> None:
    path, index = _paths(day)
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    times = []
    if index.is_file():
        try:
            times = json.loads(index.read_text(encoding="utf-8")).get("times", [])
        except (OSError, json.JSONDecodeError):
            times = []
    times.append({"taken_at": payload["taken_at"], "label": payload["label"], "count": payload["count"]})
    index.write_text(json.dumps({"date": day.isoformat(), "times": times[-50:]}, ensure_ascii=False), encoding="utf-8")


def load_snapshot(day: date | None = None) -> dict | None:
    base = manual_dir()
    if day is None:
        files = sorted(p for p in base.glob("????-??-??.json"))
        if not files:
            return None
        path = files[-1]
    else:
        path = _paths(day)[0]
        if not path.is_file():
            return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload.get("rows", []):
        if isinstance(row.get("trade_date"), str):
            row["trade_date"] = date.fromisoformat(row["trade_date"][:10])
    index = _paths(date.fromisoformat(payload["date"]))[1]
    payload["times"] = json.loads(index.read_text(encoding="utf-8")).get("times", []) if index.is_file() else []
    return payload


def _official_exists(session, day: date) -> bool:
    return (
        session.query(DailyQuote.id)
        .filter(DailyQuote.trade_date == day, ~DailyQuote.source.in_(intraday.PROVISIONAL_SOURCES) | DailyQuote.source.is_(None))
        .first()
        is not None
    )


@contextmanager
def snapshot_day(session, payload: dict):
    """把已存的（已换算过的）快照当临时日线插进去，yield (评估日, 快照信息)，出来时删掉。"""
    day = date.fromisoformat(payload["date"])
    info = {k: payload.get(k) for k in ("kind", "taken_at", "label", "elapsed_minutes", "volume_fraction", "volume_estimated", "count")}
    info["manual"] = True
    if not payload.get("rows") or _official_exists(session, day):
        # 当天正式日线已同步：正式日线取代快照
        info.update(kind="daily", superseded=True, label=f"{day.isoformat()} 正式日线（快照已被取代）")
        yield day, info
        return
    intraday.purge_provisional(session)
    try:
        intraday._insert(session, day, payload["rows"], payload["kind"])
        yield day, info
    finally:
        session.rollback()
        intraday.purge_provisional(session)


def capture(moment: datetime | None = None, *, top: int = 100) -> dict:
    """同步执行一次完整链路，返回结果（同时存盘）。"""
    from backend.app.core.database import session_scope

    started = _time.monotonic()
    moment = (moment or now()).astimezone(SHANGHAI)
    plan = classify(moment)
    if plan["mode"] == "none":
        raise NoDataYet(f"{plan['reason']}。可以改用上一交易日 {plan['fallback'].isoformat()} 的数据。")
    day = plan["day"]
    with session_scope() as session:
        if _official_exists(session, day):
            raise NoDataYet(f"今天（{day.isoformat()}）的正式日线已经同步，直接看总览即可，不需要再采快照")
        codes = intraday.universe(session)
    _set(phase="fetch", message=f"正在拉取全市场实时报价（{len(codes)} 只）")
    rows = intraday._clean_rows(_fetch(codes), day)
    if not rows:
        raise RuntimeError("没有拿到今天的报价（行情源都没返回今天的数据）")
    kind = "close" if plan["mode"] == "close" else MANUAL_KIND
    fraction = plan["fraction"]
    _set(phase="normalize", message="按已过交易时间换算全天量能")
    rows = normalize(rows, fraction)
    hhmm = moment.strftime("%H:%M")
    label = f"{day.isoformat()} 收盘（15:00）" if kind == "close" else f"盘中快照 截至 {hhmm}"
    payload = {
        "date": day.isoformat(),
        "kind": kind,
        "taken_at": moment.isoformat(timespec="seconds"),
        "label": label,
        "elapsed_minutes": round(plan["elapsed"], 1),
        "volume_fraction": round(fraction, 4),
        "volume_estimated": fraction < 0.999,
        "count": len(rows),
        "rows": rows,
    }
    if _CANCEL.is_set():
        raise InterruptedError("已取消")
    _set(phase="score", message="和样本池比相似度、算综合评分")
    payload["result"] = build_result(payload, top)
    payload["result"]["elapsed_seconds"] = round(_time.monotonic() - started, 1)
    save_snapshot(day, payload)
    return public(payload)


def build_result(payload: dict, top: int = 100) -> dict:
    from backend.app.core.database import session_scope
    from backend.app.services.backtest_service import band_lifts, prediction_for
    from backend.app.services.ranking_service import candidates
    from backend.app.services.similarity_service import feed_order

    with session_scope() as session:
        try:
            lifts, _bt = band_lifts(session)  # 先在正式日线上算（有缓存），临时日线不进回测
        except Exception as exc:
            logger.warning("档位倍率没算出来：%s", exc)
            lifts = {}
        with snapshot_day(session, payload) as (day, info):
            pool = candidates(session, day, top)
    items = []
    for item in pool["items"]:
        formula = item.get("formula") or {}
        items.append({
            "code": item["code"], "name": item.get("name", ""), "board_name": item.get("board_name"),
            "max_similarity": item.get("max_similarity"), "avg_similarity": item.get("avg_similarity"),
            "sample_count": item.get("sample_count"), "high_similarity_count": item.get("high_similarity_count"),
            "band": item.get("band"), "is_limit_up": item.get("is_limit_up"), "change_percent": item.get("change_percent"),
            "best_match": item.get("best_match"), "carry_rate": item.get("carry_rate"),
            "composite_score": formula.get("composite_score") if formula.get("has_signal") else None,
            "level": formula.get("level"), "contributions": formula.get("contributions"), "risk_penalty": formula.get("risk_penalty"),
            "close": (item.get("bars") or [{}])[-1].get("close") if item.get("bars") else None,
            **prediction_for(item, lifts),
        })
    return {
        "date": pool.get("date") or payload["date"],
        "pool_dates": pool["pool_dates"], "pool_size": pool["pool_size"],
        "skipped_count": len(pool.get("skipped") or []), "target_skips": pool.get("target_skips"),
        "feed_order": [i["code"] for i in feed_order(items)],
        "items": items,
    }


def fallback_result(top: int = 100) -> dict:
    """开盘前 / 休市：改用最新正式日线（上一交易日）选前 100，不存快照。"""
    from backend.app.core.database import session_scope

    with session_scope() as session:
        days = quote_dates(session)
    if not days:
        raise LookupError("还没有行情，请先同步")
    payload = {"date": days[-1].isoformat(), "kind": "daily", "taken_at": None, "label": f"{days[-1].isoformat()} 正式日线（上一交易日）",
               "elapsed_minutes": None, "volume_fraction": 1.0, "volume_estimated": False, "count": 0, "rows": []}
    payload["result"] = build_result(payload, top)
    return public(payload)


def public(payload: dict) -> dict:
    out = {k: v for k, v in payload.items() if k != "rows"}
    return out


def start(moment: datetime | None = None) -> dict:
    with _LOCK:
        if STATE["running"]:
            return dict(STATE)
        STATE.update(running=True, phase="start", done=0, total=0, message="开始采集", error="",
                     started_at=now().isoformat(timespec="seconds"), result_date=None, taken_at=None, elapsed_seconds=None)
    _CANCEL.clear()

    def run():
        try:
            result = capture(moment)
            _set(phase="done", message=f"完成：{result['label']}，前 {len(result['result']['items'])} 只",
                 result_date=result["date"], taken_at=result["taken_at"], elapsed_seconds=result["result"]["elapsed_seconds"])
            from backend.app.services.wecom import notify

            notify.capture_done(result)
        except InterruptedError:
            _set(phase="cancelled", message="已取消", error="")
        except Exception as exc:
            logger.warning("手动采集失败：%s", exc)
            _set(phase="error", message=str(exc), error=str(exc))
        finally:
            _set(running=False)

    threading.Thread(target=run, name="manual-capture", daemon=True).start()
    return status()


def reset_state() -> None:
    _CANCEL.clear()
    with _LOCK:
        STATE.update(running=False, phase="idle", done=0, total=0, message="", error="", started_at=None,
                     result_date=None, taken_at=None, elapsed_seconds=None)
