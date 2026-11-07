"""多模型讨论的联网证据采集：多源、按评估日做时间窗口、按类别结构化。

优先走公开 JSON/数据接口，HTML 只在接口不可用时解析关键字段；每条证据带
source/url/fetched_at/status/category/window。单源失败只降级，不让整场讨论崩掉。
没有拿到的字段明确写「未取到」或保留 None，不把接口没有的数字推出来。

时间窗口规则：
- 日 K / 资金流取“评估日止”的最近约 30 个交易日（end 只决定上限，不按自然日截断）；
- 事件 / 资讯 / 研报 / 分时只保留 [评估日-30 自然日, 评估日] 且不晚于评估日的记录；
- 实时快照不可回溯时必须标注实际日期，不能与历史收盘直接判一致；
- 财报只选评估日前已披露的最新报告。
"""

from __future__ import annotations

import json
import math
import threading
import time
from functools import wraps
import re
from concurrent.futures import wait
from datetime import date, datetime, timedelta

import httpx

USER_AGENT = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
PUSH2_URL = "https://push2.eastmoney.com/api/qt/stock/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TRENDS_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

SINA_SPOT_URL = "https://hq.sinajs.cn/list="
SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}

CATEGORY_LABELS = {
    "market": "行情与图形",
    "market_intraday": "分时/分钟",
    "capital": "资金与交易",
    "trade": "大宗/回购/增减持/解禁",
    "news": "资讯快讯",
    "announcement": "公司公告",
    "research": "研报观点",
    "financial": "最新财报与财务分析",
}


def _client():
    # 每类调用使用自己的短连接；超时收紧到秒级，防止一次开会卡死。
    return httpx.Client(
        timeout=httpx.Timeout(connect=3.0, read=float(getattr(_AUDIT, "request_timeout", 4)), write=3.0, pool=3.0),
        headers=USER_AGENT,
        follow_redirects=False,
    )


_AUDIT = threading.local()


class UpstreamError(RuntimeError):
    pass


def _get(client, url, **kwargs):
    """Two attempts at most; never retry auth/anti-bot responses or bypass TLS."""
    from .source_registry import safe_url
    safe_url(url)
    history = getattr(_AUDIT, "history", [])
    _AUDIT.history = history
    for attempt in (1, 2):
        try:
            response = client.get(url, **kwargs)
            status = response.status_code
            headers = getattr(response, "headers", {})
            try:
                response_url = str(response.url)
            except (AttributeError, RuntimeError):
                response_url = url
            entry = {"url": response_url, "status_code": status,
                     "content_type": headers.get("content-type", "unknown"), "attempt": attempt}
            history.append(entry)
            busy = False
            try:
                body = response.json()
                busy = isinstance(body, dict) and body.get("code") == 9701
            except Exception:
                pass
            if attempt == 1 and (status in {429, 500, 502, 503, 504} or busy):
                time.sleep(.25)
                continue
            return response
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            history.append({"url": url, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"[:240]})
            if attempt == 2:
                raise
            time.sleep(.25)


def _json(response):
    status = response.status_code
    ct = getattr(response, "headers", {}).get("content-type", "unknown")
    if status != 200:
        raise UpstreamError(f"HTTP {status}，Content-Type={ct}")
    try:
        return response.json()
    except (ValueError, TypeError) as exc:
        raise UpstreamError(f"HTTP {status}，Content-Type={ct}，返回空内容或非JSON，不能当作空记录") from exc


def _push_data(response):
    payload = _json(response)
    if not isinstance(payload, dict) or payload.get("rc", 0) != 0:
        raise UpstreamError("行情接口业务错误，不能当作空记录：" + str(payload)[:160])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise UpstreamError("行情接口缺data对象，未取得数据，不能判定为无记录")
    return data


def _audited(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        _AUDIT.history = []
        started = time.monotonic()
        result = function(*args, **kwargs)
        result["http_attempts"] = list(_AUDIT.history)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if result.get("digest", {}).get("bars"):
            bars = result["digest"]["bars"]
            result["observed_interval"] = {"start": bars[0].get("date"), "end": bars[-1].get("date"), "count": len(bars)}
        return result
    return wrapped


def _financial_window(end):
    return {"kind": "latest_disclosed_report", "as_of": _eval_day(end).isoformat(),
            "start": None, "end": _eval_day(end).isoformat(), "days": None,
            "note": "最新已披露定期报告，不限近30天；报告期与披露日分开核验"}


def _market(code: str) -> str:
    return "1" if code.startswith(("6", "9")) else "0"


def _secucode(code: str) -> str:
    market = _market(code)
    return f"{code}.{'SH' if market == '1' else 'SZ'}"


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _eval_day(end: date | None) -> date:
    return end or date.today()


def _window(end: date | None) -> dict:
    stop = _eval_day(end)
    start = stop - timedelta(days=29)
    return {"start": start.isoformat(), "end": stop.isoformat(), "days": 30, "natural_days": True, "as_of": stop.isoformat()}


def _date_in_natural_window(value, end: date | None, *, date_keys: tuple[str, ...] = ()) -> tuple[date | None, bool]:
    """返回 (解析出的日期, 是否明确位于 [end-30天, end])。

    日期无法解析时按“未核验”处理，绝不冒充窗口内证据。
    """
    day = _as_date(value)
    if day is None:
        return None, False
    stop = _eval_day(end)
    return day, stop - timedelta(days=29) <= day <= stop


def _filter_natural_window(items: list[dict], end: date | None, *, date_keys: tuple[str, ...]) -> list[dict]:
    """返回 (在窗口内的 item, 未核验日期的 item) 分开标注。"""
    kept: list[dict] = []
    for item in items:
        raw = next((item.get(k) for k in date_keys if item.get(k)), None)
        day, ok = _date_in_natural_window(raw, end)
        item = dict(item)
        if day is None:
            item["window_status"] = "date_unverified"
        elif ok:
            item["window_status"] = "in_window"
        else:
            item["window_status"] = "outside_window"
        kept.append(item)
    return kept


def _latest_before_disclosure(rows: list[dict], end: date | None, *, report_keys: tuple[str, ...], notice_keys: tuple[str, ...]) -> dict | None:
    """最新一份在评估日前已披露的定期报告；披露日未知时不能确认为评估日前已披露。"""
    stop = _eval_day(end)
    candidates: list[tuple[date | None, dict]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        report_day = next((_as_date(row.get(k)) for k in report_keys if row.get(k)), None)
        notice_day = next((_as_date(row.get(k)) for k in notice_keys if row.get(k)), None)
        if notice_day is None or notice_day > stop or report_day is None or report_day > stop:
            continue
        sort_key = report_day
        candidates.append((sort_key, row))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: (pair[0], _row_date_for_tiebreak(pair[1])), reverse=True)
    return candidates[0][1]


def _row_date_for_tiebreak(row: dict) -> str:
    for key in ("REPORT_DATE", "NOTICE_DATE", "DATE", "UPDATE_DATE", "date"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def _ok(category: str, source: str, url: str, digest: dict | list | str | None, fetched_at: str, window: dict | None = None) -> dict:
    return {
        "status": "ok",
        "category": category,
        "source": source,
        "url": url,
        "fetched_at": fetched_at,
        "window": window,
        "digest": digest,
    }


def _empty(category: str, source: str, url: str, note: str, fetched_at: str, window: dict | None = None) -> dict:
    return {
        "status": "empty",
        "category": category,
        "source": source,
        "url": url,
        "fetched_at": fetched_at,
        "window": window,
        "digest": {"note": note},
    }


def _fail(category: str, source: str, url: str, reason: str, fetched_at: str, window: dict | None = None) -> dict:
    return {
        "status": "failed",
        "category": category,
        "source": source,
        "url": url,
        "fetched_at": fetched_at,
        "window": window,
        "digest": {"reason": reason[:240]},
    }


def _text(response: httpx.Response, limit: int = 300000) -> str:
    return (response.text or "")[:limit]


def _num(value):
    if isinstance(value, bool) or value in (None, "", "-", "--"):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _date_str(value) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text[:10] if text else None


def _compact_float(value) -> float | None:
    value = _num(value)
    return None if value is None else round(value, 4)


def _first_row(payload: dict, key: str) -> dict:
    data = payload.get("data") or {}
    rows = data.get(key) or []
    return rows[0] if rows else {}


def _request_json(client: httpx.Client, url: str, params: dict, source: str, category: str, window: dict | None = None) -> dict:
    response = _get(client, url, params=params)
    if response.status_code != 200:
        return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"响应不是 JSON：{type(exc).__name__}", _now(), window)


def fetch_spot(code: str, end: date | None = None) -> dict:
    """公开行情快照：标注实际返回日期；不能回溯时不得与历史收盘直接判一致。"""
    category, source, url = "market", "eastmoney_spot", PUSH2_URL
    window = _window(end)
    try:
        with _client() as client:
            response = _get(client, url, params={
                "secid": f"{_market(code)}.{code}",
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170",
                "fltt": 2,
                "invt": 2,
            })
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        data = (response.json() or {}).get("data")
        if not data:
            return _empty(category, source, url, "接口没有返回快照", _now(), window)
        digest = {
            "code": str(data.get("f57") or code),
            "name": data.get("f58"),
            "actual_date": str(data.get("f86") or "")[:10] or None,
            "price": _compact_float(data.get("f43")),
            "pct_chg": _compact_float(data.get("f170")),
            "high": _compact_float(data.get("f44")),
            "low": _compact_float(data.get("f45")),
            "open": _compact_float(data.get("f46")),
            "pre_close": _compact_float(data.get("f60")),
            "volume": _compact_float(data.get("f47")),
            "amount": _compact_float(data.get("f48")),
            "comparison_note": "实时快照不可回溯；actual_date 为空时无法核验对应交易日，不得与历史收盘直接判一致。",
        }
        return _ok(category, source, url, digest, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_sina_spot(code: str, end: date | None = None) -> dict:
    """独立源（新浪）公开行情快照，保留与东财的来源差异。"""
    category, source, url = "market", "sina_spot", SINA_SPOT_URL
    window = _window(end)
    try:
        from backend.app.providers.markets import symbol_of

        symbol = symbol_of(code)
        with _client() as client:
            client.headers.update(SINA_HEADERS)
            response = _get(client, url + symbol)
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        match = re.search(r'hq_str_[^=]+="([^"]*)"', response.text or "")
        if not match:
            return _empty(category, source, url, "接口没有返回快照", _now(), window)
        parts = match.group(1).split(",")
        if len(parts) < 10 or not parts[0]:
            return _empty(category, source, url, "接口没有返回快照", _now(), window)
        digest = {
            "code": code,
            "name": parts[0],
            "actual_date": parts[30].strip()[:10] if len(parts) > 30 and parts[30] else None,
            "open": _compact_float(parts[1]),
            "pre_close": _compact_float(parts[2]),
            "price": _compact_float(parts[3]),
            "high": _compact_float(parts[4]),
            "low": _compact_float(parts[5]),
            "volume": _compact_float(parts[8]),
            "amount": _compact_float(parts[9]),
            "comparison_note": "新浪实时快照不可回溯；actual_date 为空时无法核验对应交易日。",
        }
        return _ok(category, source, url, digest, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_kline(code: str, end: date | None = None, limit: int = 35) -> dict:
    """近约 30 个交易日日 K，含数值序列，供前端画图和模型阅读。"""
    category, source, url = "market", "eastmoney_kline", KLINE_URL
    window = _window(end)
    try:
        start = _eval_day(end) - timedelta(days=100)
        params = {
            "secid": f"{_market(code)}.{code}",
            "klt": 101,
            "fqt": 0,
            "beg": start.strftime("%Y%m%d"),
            "end": (end or date.today()).strftime("%Y%m%d"),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "lmt": limit,
        }
        with _client() as client:
            response = _get(client, url, params=params)
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        lines = _push_data(response).get("klines") or []
        bars = []
        for line in lines:
            parts = str(line).split(",")
            if len(parts) < 11:
                continue
            bars.append({
                "date": parts[0][:10],
                "open": _compact_float(parts[1]),
                "close": _compact_float(parts[2]),
                "high": _compact_float(parts[3]),
                "low": _compact_float(parts[4]),
                "volume": (_compact_float(parts[5]) * 100 if _compact_float(parts[5]) is not None else None),
                "amount": _compact_float(parts[6]),
                "pct_chg": _compact_float(parts[8]),
                "turnover_rate": _compact_float(parts[10]),
            })
        if not bars:
            return _empty(category, source, url, "未取到日K线", _now(), window)
        bars = [bar for bar in bars if bar["date"] <= _eval_day(end).isoformat()][-30:]
        trading_window = {"kind": "trading_days", "start": bars[0]["date"] if bars else None,
                          "end": _eval_day(end).isoformat(), "requested_trading_days": 30, "observed_count": len(bars)}
        return _ok(category, source, url, {"count": len(bars), "bars": bars, "volume_unit": "股"}, _now(), trading_window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_intraday(code: str, end: date | None = None, ndays: int = 5) -> dict:
    """近几日分时/分钟摘要，只保留条数和最近几条，不把整个分钟序列塞进提示词。"""
    category, source, url = "market_intraday", "eastmoney_trends", TRENDS_URL
    try:
        params = {
            "secid": f"{_market(code)}.{code}",
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ndays": min(max(ndays, 1), 5),
            "iscr": 0,
        }
        with _client() as client:
            response = _get(client, url, params=params)
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now())
        data = _push_data(response)
        lines = [line for line in (data.get("trends") or []) if str(line)[:10] <= _eval_day(end).isoformat()]
        last = []
        for line in lines[-5:]:
            parts = str(line).split(",")
            if len(parts) < 8:
                continue
            last.append({
                "datetime": parts[0],
                "price": _compact_float(parts[2]),
                "high": _compact_float(parts[3]),
                "volume": (_compact_float(parts[5]) * 100 if _compact_float(parts[5]) is not None else None),
                "amount": _compact_float(parts[6]),
                "avg_price": _compact_float(parts[7]),
            })
        if not lines:
            return _empty(category, source, url, "未取到分时/分钟数据", _now())
        return _ok(category, source, url, {"points": len(lines), "last": last}, _now())
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now())


def fetch_capital_flow(code: str, end: date | None = None, limit: int = 35) -> dict:
    """东财主力/超大单/大单/中单/小单日度资金流序列。"""
    category, source, url = "capital", "eastmoney_capital_flow", FFLOW_URL
    window = _window(end)
    try:
        params = {
            "secid": f"{_market(code)}.{code}",
            "klt": 101,
            "lmt": limit,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63",
        }
        with _client() as client:
            response = _get(client, url, params=params)
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        lines = _push_data(response).get("klines") or []
        rows = []
        for line in lines:
            parts = str(line).split(",")
            if len(parts) < 11:
                continue
            rows.append({
                "date": parts[0],
                "main_net": _compact_float(parts[1]),
                "small_net": _compact_float(parts[2]),
                "medium_net": _compact_float(parts[3]),
                "large_net": _compact_float(parts[4]),
                "super_net": _compact_float(parts[5]),
                "main_net_pct": _compact_float(parts[6]),
                "small_net_pct": _compact_float(parts[7]),
                "medium_net_pct": _compact_float(parts[8]),
                "large_net_pct": _compact_float(parts[9]),
                "super_net_pct": _compact_float(parts[10]),
            })
        rows = [row for row in rows if _date_in_natural_window(row.get("date"), end)[1]]
        if not rows:
            return _empty(category, source, url, "未取到资金流向", _now(), window)
        return _ok(category, source, url, {"count": len(rows), "rows": rows}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def _dc_filter(field: str, code: str) -> str:
    return f'({field}="{code}")'


_DC_SCHEMAS = {
    "RPT_DAILYBILLBOARD_DETAILSNEW": ("SECURITY_CODE", "TRADE_DATE"),
    "RPTA_WEB_RZRQ_GGMX": ("SCODE", "DATE"),
    "RPT_MUTUAL_HOLDSTOCKNORTH_STA": ("SECURITY_CODE", "TRADE_DATE"),
    "RPT_DATA_BLOCKTRADE": ("SECURITY_CODE", "TRADE_DATE"),
    "RPTA_WEB_GETHGLIST_NEW": ("DIM_SCODE", "DIM_DATE"),
    "RPT_SHARE_HOLDER_INCREASE": ("SECURITY_CODE", "END_DATE"),
    "RPT_LIFT_STAGE": ("SECURITY_CODE", "FREE_DATE"),
    "RPT_RESEARCH_REPORT": ("SECURITY_CODE", "PUBLISH_DATE"),
    "RPT_DMSK_FN_INCOME": ("SECURITY_CODE", "REPORT_DATE"),
    "RPT_DMSK_FN_BALANCE": ("SECURITY_CODE", "REPORT_DATE"),
    "RPT_DMSK_FN_CASHFLOW": ("SECURITY_CODE", "REPORT_DATE"),
    "RPT_LICO_FN_CPD": ("SECURITY_CODE", "REPORTDATE"),
}


def _datacenter_rows(client: httpx.Client, report_name: str, filter_text: str, page_size: int, columns: str = "ALL") -> tuple[dict | None, list]:
    if report_name not in _DC_SCHEMAS:
        raise UpstreamError(f"未验证的数据表：{report_name}，停止猜测查询")
    _, sort = _DC_SCHEMAS[report_name]
    response = _get(client, DATACENTER_URL, params={
        "sortColumns": sort, "sortTypes": "-1", "pageSize": page_size,
        "pageNumber": 1, "reportName": report_name, "columns": columns,
        "source": "WEB", "client": "WEB", "filter": filter_text,
    })
    payload = _json(response)
    if not isinstance(payload, dict):
        raise UpstreamError("DataCenter响应结构不是对象")
    if payload.get("success") is False:
        if payload.get("code") == 9201 and payload.get("message") == "返回数据为空":
            return payload, []
        raise UpstreamError(f"业务错误 code={payload.get('code')}: {payload.get('message')}（不是空记录）")
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise UpstreamError("DataCenter缺result.data，不能当作空记录")
    return payload, result["data"]


def _event(code, end, report, source, label, category="trade", note=""):
    window = _window(end)
    field, date_field = _DC_SCHEMAS[report]
    query = (_dc_filter(field, code) + f"({date_field}>='{window['start']}')({date_field}<='{window['end']}')")
    try:
        with _client() as client:
            _payload, rows = _datacenter_rows(client, report, query, 100)
        rows = [row for row in rows if _date_in_natural_window(row.get(date_field), end)[1]]
        # Historical leaderboard rows include returns after the event. Never leak
        # those outcomes into an as-of evidence package.
        rows = [{key: value for key, value in row.items()
                 if not key.startswith(("D1_", "D2_", "D3_", "D5_", "D10_", "D20_"))} for row in rows]
        if not rows:
            return _empty(category, source, DATACENTER_URL, f"有效查询在观察区间无{label}公开记录；不代表数值为0，也不能据此判定资格不适用。{note}", _now(), window)
        return _ok(category, source, DATACENTER_URL, {"count": len(rows), "rows": _latest_known_dates(rows, (date_field,), 35), "note": note}, _now(), window)
    except Exception as exc:
        return _fail(category, source, DATACENTER_URL, f"{type(exc).__name__}: {exc}", _now(), window)


def _latest_known_dates(rows: list, keys: tuple[str, ...], max_rows: int = 12) -> list:
    """从未知字段名里挑出最近日期和值，不要求精确列名；尽量把行压小。"""
    out = []
    for row in rows[:max_rows]:
        if not isinstance(row, dict):
            continue
        date_value = next((row.get(k) for k in keys if row.get(k)), None)
        item = {"date": _date_str(date_value)}
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                item[key] = _compact_float(value)
            elif isinstance(value, str) and len(value) <= 80:
                item[key] = value
        out.append(item)
    return out


def fetch_lhb(code: str, end: date | None = None) -> dict:
    return _event(code, end, "RPT_DAILYBILLBOARD_DETAILSNEW", "eastmoney_lhb", "龙虎榜", "capital")


def fetch_margin(code: str, end: date | None = None) -> dict:
    return _event(code, end, "RPTA_WEB_RZRQ_GGMX", "eastmoney_margin", "融资融券", "capital", "未核实股票融资融券资格。")


def fetch_northbound(code: str, end: date | None = None) -> dict:
    return _event(code, end, "RPT_MUTUAL_HOLDSTOCKNORTH_STA", "eastmoney_northbound", "北向/沪深港通持股", "capital", "公开持股数据可能非每日披露；缺失不能推定持股为0。")


def fetch_block_trade(code: str, end: date | None = None) -> dict:
    return _event(code, end, "RPT_DATA_BLOCKTRADE", "eastmoney_block_trade", "大宗交易")


def fetch_repurchase(code: str, end: date | None = None) -> dict:
    return _event(code, end, "RPTA_WEB_GETHGLIST_NEW", "eastmoney_repurchase", "回购")


def fetch_holder_change(code: str, end: date | None = None) -> dict:
    return _event(code, end, "RPT_SHARE_HOLDER_INCREASE", "eastmoney_holder_change", "增减持")


def fetch_unlock(code: str, end: date | None = None) -> dict:
    return _event(code, end, "RPT_LIFT_STAGE", "eastmoney_unlock", "解禁")


def fetch_news(code: str, end: date | None = None, limit: int = 12) -> dict:
    """近 30 天公开新闻/快讯标题、时间、来源、链接。"""
    category, source, url = "news", "eastmoney_news", NEWS_URL
    window = _window(end)
    keyword = code
    params = {
        "cb": "jQuery",
        "param": json.dumps({
            "uid": "",
            "keyword": keyword,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": limit, "preTag": "", "postTag": ""}},
        }, ensure_ascii=False),
    }
    try:
        with _client() as client:
            response = _get(client, url, params=params)
        text = response.text or ""
        if response.status_code != 200 or "jQuery(" not in text:
            return _fail(category, source, url, f"HTTP {response.status_code}" if response.status_code != 200 else "接口返回格式异常", _now(), window)
        payload = json.loads(text[text.find("(") + 1 : text.rfind(")")])
        rows = ((((payload or {}).get("result") or {}).get("cmsArticleWebOld") or [])[:limit])
        digest = []
        for row in rows:
            item = {
                "title": str(row.get("title") or row.get("Title") or "").strip(),
                "date": str(row.get("date") or row.get("Date") or "")[:10],
                "source": str(row.get("mediaName") or row.get("source") or ""),
                "url": str(row.get("url") or row.get("Url") or ""),
            }
            if (item["title"] or item["url"]) and _date_in_natural_window(item["date"], end)[1]:
                digest.append(item)
        if not digest:
            return _empty(category, source, url, "未取到新闻/快讯", _now(), window)
        return _ok(category, source, url, {"count": len(digest), "items": digest}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_announcements(code: str, end: date | None = None, limit: int = 12) -> dict:
    """近 30 天公司公告标题、日期、链接。"""
    category, source, url = "announcement", "eastmoney_announcements", ANN_URL
    window = _window(end)
    try:
        with _client() as client:
            response = _get(client, url, params={
                "sr": "-1",
                "page_size": limit,
                "page_index": 1,
                "ann_type": "A",
                "client_source": "web",
                "stock_list": code,
            })
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        rows = ((response.json() or {}).get("data") or {}).get("list") or []
        digest = []
        for row in rows:
            item = {
                "title": str(row.get("title") or "").strip(),
                "notice_date": str(row.get("notice_date") or "").split(" ")[0],
                "url": f"https://data.eastmoney.com/notices/detail/{code}/{row.get('art_code') or ''}.html" if row.get("art_code") else "",
            }
            if item["title"] and _date_in_natural_window(item["notice_date"], end)[1]:
                digest.append(item)
        if not digest:
            return _empty(category, source, url, "未取到公告", _now(), window)
        return _ok(category, source, url, {"count": len(digest), "items": digest}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_research(code: str, end: date | None = None, limit: int = 12) -> dict:
    url = "https://reportapi.eastmoney.com/report/list"
    window = _window(end)
    try:
        with _client() as client:
            payload = _json(_get(client, url, params={"code": code, "qType": 0, "pageNo": 1,
                            "pageSize": limit, "beginTime": window["start"], "endTime": window["end"]}))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise UpstreamError("研报接口缺data数组，不能当空记录")
        rows = [{"title": item.get("title"), "date": _date_str(item.get("publishDate")),
                 "organization": item.get("orgSName"), "url": "https://data.eastmoney.com/report/zw_stock.jshtml?infocode=" + str(item.get("infoCode") or "")}
                for item in payload["data"] if _date_in_natural_window(item.get("publishDate"), end)[1]]
        return (_ok("research", "eastmoney_research", url, {"items": rows, "count": len(rows)}, _now(), window) if rows else
                _empty("research", "eastmoney_research", url, "有效查询在观察区间无公开研报记录", _now(), window))
    except Exception as exc:
        return _fail("research", "eastmoney_research", url, f"{type(exc).__name__}: {exc}", _now(), window)


_FINANCIAL_FIELDS = {
    "revenue": "TOTAL_OPERATE_INCOME", "net_profit": "PARENT_NETPROFIT",
    "eps": "BASIC_EPS", "roe": "WEIGHTAVG_ROE", "gross_margin": "XSMLL",
    "revenue_yoy": "YSTZ", "net_profit_yoy": "SJLTZ",
    "total_assets": "TOTAL_ASSETS", "total_liabilities": "TOTAL_LIABILITIES",
    "operating_cash_flow": "NETCASH_OPERATE", "cash_equivalents": "CCE_ADD",
}


def _financial_datacenter(code, end, report, source):
    window = _financial_window(end)
    report_field = _DC_SCHEMAS[report][1]
    try:
        with _client() as client:
            _payload, rows = _datacenter_rows(client, report,
                _dc_filter("SECURITY_CODE", code) + f"(NOTICE_DATE<='{window['as_of']}')", 12)
        latest = _latest_before_disclosure(rows, end, report_keys=(report_field,), notice_keys=("NOTICE_DATE",))
        if latest is None:
            result = _empty("financial", source, DATACENTER_URL, "未取到披露日明确且不晚于评估日的定期报告；不能拿报告期替代披露日", _now(), window)
            if rows:
                result["status"] = "unverified"
            return result
        digest = {"report_date": _date_str(latest.get(report_field)), "publish_date": _date_str(latest.get("NOTICE_DATE")),
                  "currency": "CNY", "amount_unit": "元", "statement": report,
                  "units": {"revenue": "元", "net_profit": "元", "eps": "元/股", "roe": "%", "gross_margin": "%", "revenue_yoy": "%", "net_profit_yoy": "%"},
                  **{key: _compact_float(latest.get(field)) for key, field in _FINANCIAL_FIELDS.items()}}
        return _ok("financial", source, DATACENTER_URL, {"latest": digest, "rows_seen": len(rows)}, _now(), window)
    except Exception as exc:
        return _fail("financial", source, DATACENTER_URL, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_financial_main(code: str, end: date | None = None) -> dict:
    return _financial_datacenter(code, end, "RPT_DMSK_FN_INCOME", "eastmoney_financial_main")


def fetch_financial_zy(code: str, end: date | None = None) -> dict:
    return _financial_datacenter(code, end, "RPT_LICO_FN_CPD", "eastmoney_financial_zy")


def fetch_financial_balance(code: str, end: date | None = None) -> dict:
    return _financial_datacenter(code, end, "RPT_DMSK_FN_BALANCE", "eastmoney_financial_balance")


def fetch_financial_cashflow(code: str, end: date | None = None) -> dict:
    return _financial_datacenter(code, end, "RPT_DMSK_FN_CASHFLOW", "eastmoney_financial_cashflow")


def fetch_sina_financial(code: str, end: date | None = None) -> dict:
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    window = _financial_window(end)
    try:
        with _client() as client:
            response = _get(client, url, params={"paperCode": ("sh" if _market(code) == "1" else "sz") + code,
                        "source": "gjzb", "type": 0, "page": 1, "num": 12})
        payload = _json(response)
        result = payload.get("result") or {}
        if (result.get("status") or {}).get("code") != 0:
            raise UpstreamError("新浪财报业务返回失败：" + str(result.get("status")))
        reports = (result.get("data") or {}).get("report_list")
        if not isinstance(reports, dict):
            raise UpstreamError("新浪财报缺report_list，不能当空记录")
        rows = [{**block, "REPORT_DATE": key} for key, block in reports.items()]
        latest = _latest_before_disclosure(rows, end, report_keys=("REPORT_DATE",), notice_keys=("publish_date",))
        if latest is None:
            result = _empty("financial", "sina_financial", url, "未取到评估日前已明确披露的财报", _now(), window)
            if rows:
                result["status"] = "unverified"
            return result
        metrics = {item["item_title"]: {"value": _compact_float(item.get("item_value")), "raw_value": item.get("item_value"),
                    "unit": item.get("item_unit") or "接口未提供单位，保留原值；不得自行推断比例或换算"}
                   for item in latest.get("data", []) if item.get("item_title") and _num(item.get("item_value")) is not None}
        digest = {"report_date": _as_date(latest["REPORT_DATE"]).isoformat(),
                  "publish_date": _as_date(latest["publish_date"]).isoformat(), "currency": latest.get("rCurrency"),
                  "metrics": metrics, "audit_status": latest.get("is_audit")}
        return _ok("financial", "sina_financial", url, {"latest": digest}, _now(), window)
    except Exception as exc:
        return _fail("financial", "sina_financial", url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_tencent_kline(code: str, end: date | None = None, limit: int = 35) -> dict:
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    symbol = ("sh" if _market(code) == "1" else "sz") + code
    window = {"kind": "trading_days", "end": _eval_day(end).isoformat(), "requested_trading_days": 30}
    try:
        with _client() as client:
            payload = _json(_get(client, url, params={"param": f"{symbol},day,,{_eval_day(end).isoformat()},60,"}))
        if payload.get("code") != 0:
            raise UpstreamError("腾讯K线业务错误：" + str(payload.get("msg")))
        raw = ((payload.get("data") or {}).get(symbol) or {}).get("day")
        if not isinstance(raw, list):
            raise UpstreamError("腾讯缺未复权day数组，不拿复权数据冒充原始价格")
        bars = [{"date": row[0], "open": _num(row[1]), "close": _num(row[2]), "high": _num(row[3]),
                 "low": _num(row[4]), "volume": _num(row[5])*100 if _num(row[5]) is not None else None,
                 "amount": None, "turnover_rate": None, "volume_unit": "股"}
                for row in raw if len(row) >= 6 and str(row[0]) <= _eval_day(end).isoformat()][-30:]
        if not bars:
            return _empty("market", "tencent_kline", url, "有效查询没有评估日及以前K线", _now(), window)
        return _ok("market", "tencent_kline", url, {"bars": bars, "count": len(bars), "adjustment": "none", "volume_unit": "股"}, _now(), window)
    except Exception as exc:
        return _fail("market", "tencent_kline", url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_sina_capital(code: str, end: date | None = None) -> dict:
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
    window = _window(end)
    try:
        with _client() as client:
            response = _get(client, url, params={"page": 1, "num": 60, "sort": "opendate", "asc": 0,
                      "daima": ("sh" if _market(code) == "1" else "sz") + code})
        rows = _json(response)
        if not isinstance(rows, list):
            raise UpstreamError("新浪资金响应不是数组，可能接口受限；不当空记录")
        rows = [row for row in rows if _date_in_natural_window(row.get("opendate"), end)[1]]
        if not rows:
            return _empty("capital", "sina_capital_flow", url, "有效查询在窗口内无公开资金记录，不表示净流入为0", _now(), window)
        digest = {"rows": [{"date": row.get("opendate"), "net_amount_yuan": _num(row.get("netamount")),
                            "main_net_yuan": _num(row.get("r0_net")), "raw": row} for row in rows], "count": len(rows)}
        return _ok("capital", "sina_capital_flow", url, digest, _now(), window)
    except Exception as exc:
        return _fail("capital", "sina_capital_flow", url, f"{type(exc).__name__}: {exc}", _now(), window)


# Each direct source call gets isolated diagnostics, also outside collect().
for _name, _function in list(globals().items()):
    if _name.startswith("fetch_") and callable(_function):
        globals()[_name] = _audited(_function)


def collect(code: str, end: date | None = None) -> list[dict]:
    """Source settings/health/cache and updates are confined to the council module."""
    from .source_registry import get_registry
    return get_registry().collect(code, end)
