"""流通股本（股）。从腾讯行情字段 72 取（缺时用字段 44 流通市值亿元 ÷ 现价），按天缓存到数据目录。

用途：新浪/腾讯日线不带换手率时，换手率 = 成交量（股）÷ 流通股本 × 100。
股本按今天的值套用到近 N 日，送转/解禁当期会有偏差，所以记为估算值。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from backend.app.core.logging import get_logger

logger = get_logger("float_shares")
URL = "https://qt.gtimg.cn/q="
BATCH = 60
_LOCK = threading.Lock()
_CACHE: dict = {"day": None, "values": {}}
_fetcher = None


def set_fetcher(func=None) -> None:
    """测试用：func(codes) -> {code: shares}。"""
    global _fetcher
    _fetcher = func
    with _LOCK:
        _CACHE.update(day=None, values={})


def _path():
    from backend.app.core.config import data_dir

    return data_dir() / "float_shares.json"


def parse(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in text.split(";"):
        if "~" not in line:
            continue
        parts = line.split("~")
        if len(parts) < 45:
            continue
        code = parts[2].strip()
        shares = None
        try:
            if len(parts) > 72 and parts[72]:
                shares = float(parts[72])
        except ValueError:
            shares = None
        if not shares:
            try:
                cap, price = float(parts[44]), float(parts[3])
                shares = cap * 1e8 / price if price > 0 else None
            except (ValueError, IndexError):
                shares = None
        if code and shares and shares > 0:
            values[code] = shares
    return values


def _symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def _fetch_batch(codes: list[str]) -> dict[str, float]:
    from backend.app.providers.http import shared_client

    try:
        with shared_client("float_shares", {"Referer": "https://gu.qq.com/"}) as client:
            response = client.get(URL + ",".join(_symbol(code) for code in codes))
        if response.status_code != 200:
            return {}
        return parse(response.content.decode("gbk", "ignore"))
    except Exception as exc:
        logger.info("流通股本没拉到：%s", exc)
        return {}


def _load() -> None:
    today = date.today().isoformat()
    if _CACHE["day"] == today:
        return
    values: dict = {}
    try:
        payload = json.loads(_path().read_text(encoding="utf-8"))
        values = {k: float(v) for k, v in (payload.get("values") or {}).items()}
    except Exception:
        values = {}
    _CACHE.update(day=today, values=values)


def _save() -> None:
    try:
        _path().write_text(json.dumps({"day": _CACHE["day"], "values": _CACHE["values"]}), encoding="utf-8")
    except OSError:
        pass


def prefetch(codes: list[str]) -> int:
    """同步开头批量拉一次（约 85 个请求，6 路并发）。"""
    with _LOCK:
        _load()
        missing = [c for c in codes if c not in _CACHE["values"]]
    if not missing:
        return 0
    if _fetcher is not None:
        got = _fetcher(missing)
    else:
        got = {}
        batches = [missing[i : i + BATCH] for i in range(0, len(missing), BATCH)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            for part in pool.map(_fetch_batch, batches):
                got.update(part)
    with _LOCK:
        _CACHE["values"].update(got)
        _save()
    return len(got)


def get(code: str) -> float | None:
    with _LOCK:
        _load()
        value = _CACHE["values"].get(code)
    if value is None:
        prefetch([code])
        with _LOCK:
            value = _CACHE["values"].get(code)
    return value


def fill_turnover(code: str, bars: list[dict]) -> int:
    """缺换手率的日线用流通股本算出来，返回补了几根。"""
    if not any(bar.get("turnover_rate") is None and bar.get("volume") for bar in bars):
        return 0
    shares = get(code)
    if not shares:
        return 0
    filled = 0
    for bar in bars:
        if bar.get("turnover_rate") is None and bar.get("volume"):
            bar["turnover_rate"] = round(float(bar["volume"]) / shares * 100, 4)
            bar["turnover_estimated"] = True
            filled += 1
    return filled
