"""当天上午的半天线：怎么造、11:30 之后怎么冻住、怎么活过重启。

盘中监控的形态窗口最后一天用的是**当天午盘前**那根半天 K 线。下午的报价给的是
全天累计量，事后拆不出上午那一段，所以「上午那一刻」必须当场留下副本。光靠内存不够：
进程在下午重启一次，窗口就会悄悄变成「含下午」。落盘一份当天的 JSON 才谈得上冻结。

文件只是派生数据（原始行情在库里），按本项目一贯的保留策略只留当天那一份，
写入时顺手删掉别日期的文件，不无限累积。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Mapping

from backend.app.algorithms.features import Bar
from backend.app.core.clock import morning_ended, now
from backend.app.core.config import data_dir
from backend.app.core.logging import get_logger
from backend.app.services.limit_up_service import is_valid_bar

logger = get_logger("intraday")

MORNING_DIR_NAME = "intraday_morning"


def morning_dir(root: Path | None = None) -> Path:
    path = (root or data_dir()) / MORNING_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def morning_file(trade_date: date, root: Path | None = None) -> Path:
    return morning_dir(root) / f"{trade_date.isoformat()}.json"


@dataclass(frozen=True)
class CapturedMorning:
    """一次冻结的结果：上午那一刻的半天线，以及它是几点被留住的。"""

    trade_date: date
    captured_at: datetime
    bars: Mapping[str, Bar]


def morning_bar(spot: dict) -> Bar | None:
    """把一条盘中报价变成「今天到目前为止」的半天线。

    五个价格是**截至此刻**的今开 / 今高 / 今低 / 现价 / 昨收，量能是累计量。
    缺任何一个价格，或者这根线本身不合法，就返回 None——宁可不进窗口，
    也不拿现价凑一根十字星上去。
    """
    values = (spot.get("open"), spot.get("high"), spot.get("low"), spot.get("close"), spot.get("pre_close"))
    if any(value is None or float(value) <= 0 for value in values):
        return None
    day = spot.get("trade_date")
    if not isinstance(day, date):
        return None
    volume = spot.get("volume")
    amount = spot.get("amount")
    if not volume and not amount:
        return None
    bar = Bar(
        trade_date=day,
        open=float(values[0]),
        high=float(values[1]),
        low=float(values[2]),
        close=float(values[3]),
        pre_close=float(values[4]),
        volume=None if volume is None else float(volume),
        amount=None if amount is None else float(amount),
        turnover_rate=spot.get("turnover_rate"),
        is_partial=True,
    )
    if not is_valid_bar(bar.open, bar.high, bar.low, bar.close, bar.volume):
        return None
    return bar


def _bar_to_dict(bar: Bar) -> dict:
    return {
        "trade_date": bar.trade_date.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "pre_close": bar.pre_close,
        "volume": bar.volume,
        "amount": bar.amount,
        "turnover_rate": bar.turnover_rate,
    }


def _bar_from_dict(payload: dict) -> Bar | None:
    """从落盘 JSON 还原半天线。字段残缺一律当「没有这份冻结」，不猜。"""
    try:
        bar = Bar(
            trade_date=date.fromisoformat(str(payload["trade_date"])),
            open=float(payload["open"]),
            high=float(payload["high"]),
            low=float(payload["low"]),
            close=float(payload["close"]),
            pre_close=None if payload.get("pre_close") is None else float(payload["pre_close"]),
            volume=None if payload.get("volume") is None else float(payload["volume"]),
            amount=None if payload.get("amount") is None else float(payload["amount"]),
            turnover_rate=None if payload.get("turnover_rate") is None else float(payload["turnover_rate"]),
            is_partial=True,
        )
    except (KeyError, TypeError, ValueError):
        return None
    return bar if is_valid_bar(bar.open, bar.high, bar.low, bar.close, bar.volume) else None


def capture_morning_bars(
    trade_date: date,
    bars: Mapping[str, Bar],
    *,
    root: Path | None = None,
    moment: datetime | None = None,
) -> bool:
    """上午：把这一刻的半天线写进当天文件（覆盖 = 跟着上午走势一起更新）。

    11:30 之后不再写，已有文件原样留着当冻结值。这条判据就是「冻结」本身：
    去掉它，下午每一轮都会把窗口往后拖。写盘失败只记日志不抛——
    盘中监控不能因为磁盘抖一下就没榜。
    """
    if morning_ended(moment) or not bars:
        return False
    path = morning_file(trade_date, root)
    document = {
        "trade_date": trade_date.isoformat(),
        "captured_at": (moment or now()).isoformat(timespec="seconds"),
        "bar_count": len(bars),
        "bars": {code: _bar_to_dict(bar) for code, bar in bars.items()},
    }
    try:
        _drop_other_days(path.parent, trade_date)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        logger.warning("上午半天线落盘失败（%s）：%s", path, exc)
        return False
    return True


def load_morning_bars(trade_date: date, *, root: Path | None = None) -> CapturedMorning | None:
    """读当天冻结的半天线。没有文件 / 文件坏了 / 日期对不上都返回 None。

    坏文件绝不抛：这是每轮快刷都会走的路径，一个残缺 JSON 不该让整张榜消失。
    """
    path = morning_file(trade_date, root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("trade_date") != trade_date.isoformat():
        return None
    raw = document.get("bars")
    if not isinstance(raw, dict) or not raw:
        return None
    bars = {code: bar for code, bar in ((key, _bar_from_dict(value)) for key, value in raw.items()) if bar is not None}
    if not bars:
        return None
    try:
        captured_at = datetime.fromisoformat(str(document.get("captured_at")))
    except (TypeError, ValueError):
        # 时间戳读不出来不影响半天线本身，只是页面上那句「冻结于」说不准，
        # 用文件修改时间兜底，比显示空值诚实。
        try:
            captured_at = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            captured_at = now()
    return CapturedMorning(trade_date=trade_date, captured_at=captured_at, bars=bars)


def capture_header(trade_date: date, *, root: Path | None = None) -> dict | None:
    """只读冻结文件的头部（哪天、几点留的、多少根），不还原任何 K 线。

    给状态接口用：榜还没出来时页面照样说得出「今天上午的半天线留住了没有、几点留的」，
    为此把五千多根线全还原一遍是不该付的成本。
    """
    path = morning_file(trade_date, root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("trade_date") != trade_date.isoformat():
        return None
    raw = document.get("bars")
    if not isinstance(raw, dict) or not raw:
        return None
    return {
        "trade_date": trade_date.isoformat(),
        "captured_at": str(document.get("captured_at") or ""),
        "bar_count": len(raw),
    }


def _drop_other_days(directory: Path, keep: date) -> None:
    """只留当天那一份：昨天的半天线对今天的窗口没有任何意义。"""
    for stale in directory.glob("*.json"):
        if stale.stem == keep.isoformat():
            continue
        try:
            stale.unlink()
        except OSError:
            continue
