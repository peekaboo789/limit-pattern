"""新浪行情。日线和快照的成交量已经是股，成交额是元，这里不再放大。"""

from __future__ import annotations

import json
import re
from datetime import date

import httpx

from backend.app.providers.eastmoney import ProviderError
from backend.app.providers.markets import accept_instrument, normalize_code, symbol_of

KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
LIST_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SPOT_URL = "https://hq.sinajs.cn/list="
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
_ROW = re.compile(r'hq_str_(\w+)="([^"]*)"')


def _num(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_spot(text: str) -> list[dict]:
    rows: list[dict] = []
    for match in _ROW.finditer(text or ""):
        symbol, body = match.group(1), match.group(2)
        parts = body.split(",")
        if len(parts) < 10:
            continue
        accepted = accept_instrument(symbol, "", parts[0])
        if accepted is None:
            continue
        code, _exchange = accepted
        trade_day = None
        if len(parts) > 30 and parts[30]:
            try:
                trade_day = date.fromisoformat(parts[30][:10])
            except ValueError:
                trade_day = None
        rows.append(
            {
                "code": code,
                "name": parts[0],
                "trade_date": trade_day,
                "open": _num(parts[1]),
                "pre_close": _num(parts[2]),
                "close": _num(parts[3]),
                "high": _num(parts[4]),
                "low": _num(parts[5]),
                "volume": _num(parts[8]),
                "amount": _num(parts[9]),
                "turnover_rate": None,
                "source": "sina",
            }
        )
    return rows


def parse_kline(payload) -> list[dict]:
    if not isinstance(payload, list):
        return []
    bars: list[dict] = []
    for row in payload:
        if not isinstance(row, dict) or not row.get("day"):
            continue
        bars.append(
            {
                "trade_date": date.fromisoformat(str(row["day"])[:10]),
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": _num(row.get("close")),
                "volume": _num(row.get("volume")),
                "amount": _num(row.get("amount")),
                "pre_close": None,
                "turnover_rate": None,
                "source": "sina",
            }
        )
    bars.sort(key=lambda item: item["trade_date"])
    return bars


def parse_list(payload) -> list[dict]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, list):
        return []
    found: list[dict] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        accepted = accept_instrument(str(row.get("symbol") or ""), str(row.get("code") or ""), str(row.get("name") or ""))
        if accepted is None:
            continue
        code, _exchange = accepted
        found.append(
            {
                "code": code,
                "name": str(row.get("name") or "").strip(),
                "close": _num(row.get("trade")),
                "pre_close": _num(row.get("settlement")),
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "volume": _num(row.get("volume")),
                "amount": _num(row.get("amount")),
                "turnover_rate": _num(row.get("turnoverratio")),
                "pct_chg": _num(row.get("changepercent")),
                "source": "sina",
            }
        )
    return found


def _client():
    from backend.app.providers.http import shared_client

    return shared_client("sina", HEADERS)


def list_page(payload, page_size: int = 100) -> tuple[list[tuple[str, str]], bool]:
    """解析一页列表。北交所整页被滤掉时仍要继续翻页。"""
    if isinstance(payload, str):
        text = payload.strip()
        if not text or text[0] not in "[{":
            return [], False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return [], False
    if not isinstance(payload, list) or not payload:
        return [], False
    items = [(item["code"], item["name"]) for item in parse_list(payload) if item.get("name")]
    return items, len(payload) >= page_size


def fetch_stock_list() -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    page = 1
    with _client() as client:
        while page <= 80:
            response = client.get(
                LIST_URL,
                params={"page": page, "num": 100, "sort": "symbol", "asc": 1, "node": "hs_a"},
            )
            if response.status_code != 200:
                raise ProviderError(f"新浪股票列表请求失败（HTTP {response.status_code}）")
            items, more = list_page(response.text)
            for code, name in items:
                found[code] = name
            if not more:
                break
            page += 1
    if not found:
        raise ProviderError("新浪股票列表是空的")
    return list(found.items())


def fetch_kline(code: str, start: date, end: date) -> list[dict]:
    symbol = symbol_of(code)
    count = max(5, (end - start).days + 5)
    with _client() as client:
        response = client.get(
            KLINE_URL,
            params={"symbol": symbol, "scale": 240, "ma": "no", "datalen": count},
        )
    if response.status_code != 200:
        raise ProviderError(f"新浪日线请求失败（HTTP {response.status_code}）")
    text = response.text.strip()
    if not text or text == "null":
        return []
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ProviderError("新浪日线不是 JSON") from exc
    code = normalize_code(code)
    return [bar for bar in parse_kline(payload) if start <= bar["trade_date"] <= end]


def fetch_spot(codes: list[str] | None = None) -> list[dict]:
    symbols = [symbol_of(code) for code in (codes or [])]
    if not symbols:
        return []
    with _client() as client:
        response = client.get(SPOT_URL + ",".join(symbols))
    if response.status_code != 200:
        raise ProviderError(f"新浪行情请求失败（HTTP {response.status_code}）")
    return parse_spot(response.text)
