"""东方财富行情。日线用不复权，代码先按沪深 A 股过滤。"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from backend.app.providers.markets import classify, keep_code, normalize_code

LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}


class ProviderError(RuntimeError):
    pass


def parse_stock_list(payload: dict) -> list[tuple[str, str]]:
    data = payload.get("data") or {}
    rows = data.get("diff") or []
    found: list[tuple[str, str]] = []
    for row in rows:
        code = normalize_code(str(row.get("f12") or ""))
        name = str(row.get("f14") or "").strip()
        if keep_code(code) and name:
            found.append((code, name))
    return found


def parse_kline(payload: dict) -> list[dict]:
    data = payload.get("data") or {}
    lines = data.get("klines") or []
    bars: list[dict] = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        trade_day = date.fromisoformat(parts[0][:10])
        close = float(parts[2])
        change = float(parts[9]) if len(parts) > 9 and parts[9] not in {"", "-"} else None
        pre_close = close - change if change is not None else None
        bars.append(
            {
                "trade_date": trade_day,
                "open": float(parts[1]),
                "close": close,
                "high": float(parts[3]),
                "low": float(parts[4]),
                # 东方财富日线的成交量单位是手，统一换成股。
                "volume": float(parts[5]) * 100 if parts[5] not in {"", "-"} else None,
                "amount": float(parts[6]) if len(parts) > 6 and parts[6] not in {"", "-"} else None,
                "pct_chg": float(parts[8]) if len(parts) > 8 and parts[8] not in {"", "-"} else None,
                "pre_close": pre_close,
                "turnover_rate": float(parts[10]) if len(parts) > 10 and parts[10] not in {"", "-"} else None,
            }
        )
    bars.sort(key=lambda item: item["trade_date"])
    return bars


def _client():
    from backend.app.providers.http import shared_client

    return shared_client("eastmoney", HEADERS)


def fetch_stock_list() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    page = 1
    total = None
    with _client() as client:
        while page <= 40:
            response = client.get(
                LIST_URL,
                params={
                    "pn": page,
                    "pz": 200,
                    "po": 1,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f12",
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                    "fields": "f12,f14",
                },
            )
            if response.status_code != 200:
                raise ProviderError(f"股票列表请求失败（HTTP {response.status_code}）")
            payload = response.json()
            data = payload.get("data") or {}
            batch = parse_stock_list(payload)
            if total is None:
                total = int(data.get("total") or 0)
            if not batch:
                break
            found.extend(batch)
            if total and page * 200 >= total:
                break
            page += 1
    # 分页返回值里可能夹进基金，再滤一次
    unique: dict[str, str] = {}
    for code, name in found:
        if keep_code(code):
            unique[code] = name
    if not unique:
        raise ProviderError("股票列表是空的")
    return list(unique.items())


def fetch_spot(codes: list[str] | None = None) -> list[dict]:
    """当日行情走日线最后一根。单独快照不是这条链路的必需项。"""
    if not codes:
        return []
    end = date.today()
    start = end - timedelta(days=10)
    rows: list[dict] = []
    for code in codes:
        bars = fetch_kline(code, start, end)
        if not bars:
            continue
        last = bars[-1]
        last["code"] = normalize_code(code)
        last["source"] = "eastmoney"
        rows.append(last)
    return rows


def fetch_kline(code: str, start: date, end: date) -> list[dict]:
    code = normalize_code(code)
    exchange, _board = classify(code)
    if exchange not in {"SSE", "SZSE"}:
        return []
    market = "1" if exchange == "SSE" else "0"
    with _client() as client:
        response = client.get(
            KLINE_URL,
            params={
                "secid": f"{market}.{code}",
                "klt": 101,
                "fqt": 0,
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
        )
    if response.status_code != 200:
        raise ProviderError(f"{code} 日线请求失败（HTTP {response.status_code}）")
    return parse_kline(response.json())
