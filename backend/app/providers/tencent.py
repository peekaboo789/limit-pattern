"""腾讯行情。成交量从「手」换成股，成交额从「万元」换成元。日线只用不复权。"""

from __future__ import annotations

import re
from datetime import date

import httpx

from backend.app.providers.eastmoney import ProviderError
from backend.app.providers.markets import accept_instrument, normalize_code

SPOT_URL = "https://qt.gtimg.cn/q="
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://stockapp.finance.qq.com"}
_ROW = re.compile(r'v_(\w+)="([^"]*)"')


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _day_from_qq(value: str) -> date | None:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def parse_spot(text: str) -> list[dict]:
    rows: list[dict] = []
    for match in _ROW.finditer(text or ""):
        symbol, body = match.group(1), match.group(2)
        parts = body.split("~")
        if len(parts) < 7:
            continue
        accepted = accept_instrument(symbol, parts[2] if len(parts) > 2 else "", parts[1] if len(parts) > 1 else "")
        if accepted is None:
            continue
        code, _exchange = accepted
        hands = _num(parts[36]) if len(parts) > 36 else None
        if hands is None:
            hands = _num(parts[6])
        amount = None
        mixed = parts[35] if len(parts) > 35 else ""
        if mixed.count("/") >= 2:
            amount = _num(mixed.split("/")[2])
        if amount is None and len(parts) > 37 and _num(parts[37]) is not None:
            amount = _num(parts[37]) * 10000
        rows.append(
            {
                "code": code,
                "name": parts[1] if len(parts) > 1 else "",
                "trade_date": _day_from_qq(parts[30]) if len(parts) > 30 else None,
                "open": _num(parts[5]) if len(parts) > 5 else None,
                "high": _num(parts[33]) if len(parts) > 33 else None,
                "low": _num(parts[34]) if len(parts) > 34 else None,
                "close": _num(parts[3]) if len(parts) > 3 else None,
                "pre_close": _num(parts[4]) if len(parts) > 4 else None,
                "volume": None if hands is None else hands * 100,
                "amount": amount,
                "turnover_rate": _num(parts[38]) if len(parts) > 38 else None,
                "pct_chg": _num(parts[32]) if len(parts) > 32 else None,
                "limit_up_price": _num(parts[47]) if len(parts) > 47 else None,
                "source": "tencent",
            }
        )
    return rows


def parse_kline(payload: dict) -> list[dict]:
    data = (payload or {}).get("data") or {}
    bars: list[dict] = []
    for symbol, body in data.items():
        if not isinstance(body, dict):
            continue
        accepted = accept_instrument(symbol, normalize_code(symbol))
        if accepted is None:
            continue
        code, _exchange = accepted
        # qfqday 是前复权，涨停价会被缩放，不能用。
        for row in body.get("day") or []:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            hands = _num(row[5])
            bars.append(
                {
                    "code": code,
                    "trade_date": date.fromisoformat(str(row[0])[:10]),
                    "open": _num(row[1]),
                    "close": _num(row[2]),
                    "high": _num(row[3]),
                    "low": _num(row[4]),
                    "volume": None if hands is None else hands * 100,
                    "amount": None,
                    "pre_close": None,
                    "turnover_rate": None,
                    "source": "tencent",
                }
            )
    bars.sort(key=lambda item: (item["code"], item["trade_date"]))
    return bars


def parse_rank(payload: dict) -> list[dict]:
    data = (payload or {}).get("data") or {}
    found: list[dict] = []
    for row in data.get("rank_list") or []:
        accepted = accept_instrument(str(row.get("code") or ""), "", str(row.get("name") or ""))
        if accepted is None:
            continue
        code, _exchange = accepted
        hands = _num(row.get("volume"))
        wan = _num(row.get("turnover"))
        found.append(
            {
                "code": code,
                "name": str(row.get("name") or "").strip(),
                "close": _num(row.get("zxj")),
                "volume": None if hands is None else hands * 100,
                "amount": None if wan is None else wan * 10000,
                "turnover_rate": _num(row.get("hsl")),
                "pct_chg": _num(row.get("zdf")),
                "source": "tencent",
            }
        )
    return found


def _client():
    from backend.app.providers.http import shared_client

    return shared_client("tencent", HEADERS)


def fetch_stock_list() -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    offset = 0
    total = None
    with _client() as client:
        while offset < 8000:
            response = client.get(
                RANK_URL,
                params={
                    "board_code": "aStock",
                    "sort_type": "price",
                    "direct": "down",
                    "offset": offset,
                    "count": 200,
                },
            )
            if response.status_code != 200:
                raise ProviderError(f"腾讯股票列表请求失败（HTTP {response.status_code}）")
            payload = response.json()
            batch = parse_rank(payload)
            if total is None:
                total = int(((payload.get("data") or {}).get("total") or 0))
            if not batch:
                break
            for item in batch:
                if item["name"]:
                    found[item["code"]] = item["name"]
            offset += 200
            if total and offset >= total:
                break
    if not found:
        raise ProviderError("腾讯股票列表是空的")
    return list(found.items())


def fetch_kline(code: str, start: date, end: date) -> list[dict]:
    from backend.app.providers.markets import symbol_of

    symbol = symbol_of(code)
    count = max(5, (end - start).days + 5)
    with _client() as client:
        response = client.get(KLINE_URL, params={"param": f"{symbol},day,,,{count},"})
    if response.status_code != 200:
        raise ProviderError(f"腾讯日线请求失败（HTTP {response.status_code}）")
    bars = [
        bar
        for bar in parse_kline(response.json())
        if bar["code"] == normalize_code(code) and start <= bar["trade_date"] <= end
    ]
    return bars


def fetch_spot(codes: list[str] | None = None) -> list[dict]:
    from backend.app.providers.markets import symbol_of

    symbols = [symbol_of(code) for code in (codes or [])]
    if not symbols:
        return []
    with _client() as client:
        response = client.get(SPOT_URL + ",".join(symbols))
    if response.status_code != 200:
        raise ProviderError(f"腾讯行情请求失败（HTTP {response.status_code}）")
    return parse_spot(response.text)
