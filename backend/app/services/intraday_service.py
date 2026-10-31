"""盘中快照：把今天的实时报价当作「临时日线」参加打分，算完就删，不写进正式行情。

- 交易时段：用此刻报价（不完整的当日 K 线），kind=intraday。
- 11:30 冻结：上午收盘时存一份快照文件，下午可以选它，kind=morning_freeze。
- 15:00 之后、当天正式行情同步之前：报价就是收盘价，kind=close。
- 当天正式日线已同步，或今天休市：不用快照，kind=daily。
只用此刻之前已经发生的价格，不看未来。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time

from backend.app.core.calendar import is_trading_day
from backend.app.core.clock import SHANGHAI, now
from backend.app.core.config import data_dir
from backend.app.core.logging import get_logger
from backend.app.models.tables import DailyQuote, Stock
from backend.app.providers.markets import is_in_scope
from backend.app.services.limit_up_service import limit_up_price, ratio_for
from backend.app.services.retention import quote_dates

logger = get_logger("intraday")

PROVISIONAL_SOURCES = ("intraday", "morning_freeze", "close")
BATCH = 60
_bulk_fetcher = None


def set_bulk_fetcher(func=None) -> None:
    """测试用：func(codes) -> list[dict]。"""
    global _bulk_fetcher
    _bulk_fetcher = func


def snapshots_dir():
    path = data_dir() / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fetch_batch(codes: list[str]) -> list[dict]:
    from backend.app.services.monitor_service import _default_fetch

    try:
        rows, _source = _default_fetch(codes)
        return rows
    except Exception as exc:
        logger.warning("一批快照没拉到：%s", exc)
        return []


def fetch_all(codes: list[str]) -> list[dict]:
    if _bulk_fetcher is not None:
        return _bulk_fetcher(codes)
    batches = [codes[i : i + BATCH] for i in range(0, len(codes), BATCH)]
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for part in pool.map(_fetch_batch, batches):
            rows.extend(part)
    return rows


def snapshot_kind(moment: datetime) -> str:
    clock = moment.time()
    if clock < time(9, 30):
        return "daily"
    if clock < time(11, 30):
        return "intraday"
    if clock < time(13, 0):
        return "morning_freeze"
    if clock < time(15, 0):
        return "intraday"
    return "close"


SESSION_MINUTES = 240


def elapsed_minutes(moment: datetime) -> float:
    """已过的连续竞价分钟数（9:30–11:30、13:00–15:00，午休不算），0–240。"""
    clock = moment.astimezone(SHANGHAI).time() if moment.tzinfo else moment.time()
    minutes = clock.hour * 60 + clock.minute + clock.second / 60.0
    morning = min(max(minutes - (9 * 60 + 30), 0.0), 120.0)
    afternoon = min(max(minutes - 13 * 60, 0.0), 120.0)
    return morning + afternoon


def volume_fraction(elapsed: float, profile: str = "u") -> float:
    """到此刻为止，全天成交量通常已完成的比例（估算）。

    U 形：强度 w(t)=1+1.5·((t-120)/120)²，开盘、收盘最活跃；积分归一化。linear 为线性兜底。
    """
    t = min(max(float(elapsed), 0.0), SESSION_MINUTES)
    if t <= 0:
        return 0.0
    if profile == "linear":
        return t / SESSION_MINUTES
    cumulative = t + 60.0 * (((t - 120.0) / 120.0) ** 3 + 1.0)
    return cumulative / (SESSION_MINUTES + 120.0)


def scale_partial(row: dict, fraction: float) -> dict:
    """把盘中累计量换算成全天估算值；成交额、换手率同比例放大。标记 volume_estimated。"""
    if fraction <= 0 or fraction >= 0.999:
        return row
    scaled = dict(row)
    factor = 1.0 / max(fraction, 0.02)
    for field in ("volume", "amount", "turnover_rate"):
        if scaled.get(field) is not None:
            scaled[field] = float(scaled[field]) * factor
    scaled["volume_estimated"] = True
    return scaled


def universe(session) -> list[str]:
    days = quote_dates(session)
    if not days:
        return []
    codes = [row[0] for row in session.query(DailyQuote.stock_code).filter(DailyQuote.trade_date == days[-1]).distinct()]
    return sorted(code for code in codes if is_in_scope(code))


def _clean_rows(rows: list[dict], day: date) -> list[dict]:
    kept = []
    for row in rows:
        if row.get("trade_date") not in (None, day):
            continue  # 报价还停在上一天（停牌或数据源没更新），不当作今天
        if not row.get("close") or not row.get("pre_close") or not row.get("open"):
            continue
        kept.append(row)
    return kept


def save_morning_freeze(moment: datetime | None = None) -> dict | None:
    """11:30 定时调用：把上午收盘时的报价存成文件，下午打分可以选它。"""
    from backend.app.core.database import session_scope

    moment = moment or now()
    day = moment.astimezone(SHANGHAI).date()
    if not is_trading_day(day):
        return None
    with session_scope() as session:
        codes = universe(session)
    rows = _clean_rows(fetch_all(codes), day)
    payload = {"date": day.isoformat(), "taken_at": moment.isoformat(timespec="seconds"), "rows": rows}
    path = snapshots_dir() / f"{day.isoformat()}-1130.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info("11:30 冻结快照已保存 %s 只", len(rows))
    return {"date": payload["date"], "taken_at": payload["taken_at"], "count": len(rows)}


def load_morning_freeze(day: date) -> dict | None:
    path = snapshots_dir() / f"{day.isoformat()}-1130.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload.get("rows", []):
        if isinstance(row.get("trade_date"), str):
            row["trade_date"] = date.fromisoformat(row["trade_date"][:10])
    return payload


def _insert(session, day: date, rows: list[dict], source: str) -> int:
    stocks = {row.code: row for row in session.query(Stock)}
    count = 0
    for row in rows:
        code = str(row["code"])
        if not is_in_scope(code):
            continue
        stock = stocks.get(code)
        board = (stock.board if stock else "") or "main_board"
        is_st = bool(stock.is_st) if stock else False
        pre = float(row["pre_close"])
        close = float(row["close"])
        limit = float(limit_up_price(pre, ratio_for(board, is_st, day)))
        session.add(
            DailyQuote(
                stock_code=code,
                trade_date=day,
                open=row.get("open"),
                high=row.get("high") or close,
                low=row.get("low") or close,
                close=close,
                pre_close=pre,
                volume=row.get("volume"),
                amount=row.get("amount"),
                turnover_rate=row.get("turnover_rate"),
                change_percent=round((close - pre) / pre * 100, 2),
                is_limit_up=close >= limit - 0.005,
                is_limit_down=False,
                limit_up_price=limit,
                limit_up_percent=float(ratio_for(board, is_st, day)) * 100,
                limit_up_reason="",
                source=source,
            )
        )
        count += 1
    session.commit()
    return count


def purge_provisional(session) -> None:
    session.query(DailyQuote).filter(DailyQuote.source.in_(PROVISIONAL_SOURCES)).delete(synchronize_session=False)
    session.commit()


@contextmanager
def provisional_day(session, moment: datetime | None = None, prefer: str = "auto"):
    """yield (评估日, 快照信息)。prefer=morning 时下午也用 11:30 冻结快照。"""
    moment = (moment or now()).astimezone(SHANGHAI)
    day = moment.date()
    days = quote_dates(session, day)
    info = {"kind": "daily", "taken_at": None, "count": 0}
    if not is_trading_day(day) or (days and days[-1] == day):
        yield (days[-1] if days else None), info
        return
    kind = snapshot_kind(moment)
    if kind == "daily":
        yield (days[-1] if days else None), info
        return
    rows: list[dict] = []
    taken_at = moment.isoformat(timespec="seconds")
    if kind == "morning_freeze" or (prefer == "morning" and moment.time() >= time(13, 0)):
        frozen = load_morning_freeze(day)
        if frozen:
            rows, taken_at, kind = frozen["rows"], frozen["taken_at"], "morning_freeze"
    if not rows:
        rows = _clean_rows(fetch_all(universe(session)), day)
        if kind == "morning_freeze":
            kind = "intraday"
    if not rows:
        logger.warning("没有拿到今天的报价，退回最新日线")
        yield (days[-1] if days else None), info
        return
    stamp_time = datetime.fromisoformat(taken_at) if taken_at else moment
    elapsed = elapsed_minutes(stamp_time)
    fraction = volume_fraction(elapsed) if kind != "close" else 1.0
    rows = [scale_partial(row, fraction) for row in rows]
    purge_provisional(session)
    try:
        count = _insert(session, day, rows, kind)
        yield day, {
            "kind": kind,
            "taken_at": taken_at,
            "count": count,
            "elapsed_minutes": round(elapsed, 1),
            "volume_fraction": round(fraction, 4),
            "volume_estimated": fraction < 0.999,
            "volume_note": "盘中成交量/额/换手已按 U 形日内分布换算成全天估算值（估）" if fraction < 0.999 else "",
        }
    finally:
        session.rollback()
        purge_provisional(session)
