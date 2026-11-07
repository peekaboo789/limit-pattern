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
import re
from concurrent.futures import ThreadPoolExecutor
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
F10_MAIN_TARGET_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/MainTargetAjaxNew"
F10_ZY_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew"

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
        timeout=httpx.Timeout(connect=3.0, read=6.0, write=3.0, pool=3.0),
        headers=USER_AGENT,
        follow_redirects=True,
    )


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
    start = stop - timedelta(days=30)
    return {"start": start.isoformat(), "end": stop.isoformat(), "days": 31, "natural_days": True, "as_of": stop.isoformat()}


def _date_in_natural_window(value, end: date | None, *, date_keys: tuple[str, ...] = ()) -> tuple[date | None, bool]:
    """返回 (解析出的日期, 是否明确位于 [end-30天, end])。

    日期无法解析时按“未核验”处理，绝不冒充窗口内证据。
    """
    day = _as_date(value)
    if day is None:
        return None, False
    stop = _eval_day(end)
    return day, stop - timedelta(days=30) <= day <= stop


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
    """最新一份在评估日前已披露的定期报告；披露日未知时降级用报告期做保守选择。"""
    stop = _eval_day(end)
    candidates: list[tuple[date | None, dict]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        report_day = next((_as_date(row.get(k)) for k in report_keys if row.get(k)), None)
        notice_day = next((_as_date(row.get(k)) for k in notice_keys if row.get(k)), None)
        if notice_day is not None and notice_day > stop:
            continue
        if notice_day is None and report_day is not None and report_day > stop:
            continue
        sort_key = notice_day or report_day or date.min
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
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
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
    response = client.get(url, params=params)
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
            response = client.get(url, params={
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
            response = client.get(url + symbol)
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
        start = date.fromisoformat(window["start"]) if end else date.today() - timedelta(days=45)
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
            response = client.get(url, params=params)
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        lines = ((response.json() or {}).get("data") or {}).get("klines") or []
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
                "volume": _compact_float(parts[5]),
                "amount": _compact_float(parts[6]),
                "pct_chg": _compact_float(parts[8]),
                "turnover_rate": _compact_float(parts[10]),
            })
        if not bars:
            return _empty(category, source, url, "未取到日K线", _now(), window)
        return _ok(category, source, url, {"count": len(bars), "bars": bars}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_intraday(code: str, ndays: int = 5) -> dict:
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
            response = client.get(url, params=params)
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now())
        data = (response.json() or {}).get("data") or {}
        lines = data.get("trends") or []
        last = []
        for line in lines[-5:]:
            parts = str(line).split(",")
            if len(parts) < 8:
                continue
            last.append({
                "datetime": parts[0],
                "price": _compact_float(parts[2]),
                "pct_chg": _compact_float(parts[3]),
                "volume": _compact_float(parts[5]),
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
            response = client.get(url, params=params)
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        lines = ((response.json() or {}).get("data") or {}).get("klines") or []
        rows = []
        for line in lines:
            parts = str(line).split(",")
            if len(parts) < 10:
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
        if not rows:
            return _empty(category, source, url, "未取到资金流向", _now(), window)
        return _ok(category, source, url, {"count": len(rows), "rows": rows}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def _dc_filter(field: str, code: str) -> str:
    return f'({field}="{code}")'


def _datacenter_rows(client: httpx.Client, report_name: str, filter_text: str, page_size: int, columns: str = "ALL") -> tuple[dict | None, list]:
    """通用东财 DataCenter 查询。返回 (payload, rows)，网络/格式错误由调用方处理。"""
    response = client.get(DATACENTER_URL, params={
        "sortColumns": "DATE,REPORT_DATE,TRADE_DATE",
        "sortTypes": "-1,-1,-1",
        "pageSize": page_size,
        "pageNumber": 1,
        "reportName": report_name,
        "columns": columns,
        "source": "WEB",
        "client": "WEB",
        "filter": filter_text,
    })
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    rows = ((payload or {}).get("result") or {}).get("data") or []
    return payload, rows


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
    """龙虎榜，近 30 天若出现过。"""
    category, source, url = "capital", "eastmoney_lhb", DATACENTER_URL
    window = _window(end)
    try:
        with _client() as client:
            _payload, rows = _datacenter_rows(
                client,
                "RPT_DAILYBILLBOARD_DETAILSNEW",
                _dc_filter("SECURITY_CODE", code),
                10,
            )
        if not rows:
            return _empty(category, source, url, "近 30 天未取到龙虎榜记录", _now(), window)
        digest = _latest_known_dates(rows, ("TRADE_DATE", "DATE"), 10)
        return _ok(category, source, url, {"count": len(rows), "rows": digest}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_margin(code: str, end: date | None = None) -> dict:
    """融资融券。接口字段不稳定，逐源降级。"""
    category, source, url = "capital", "eastmoney_margin", DATACENTER_URL
    window = _window(end)
    try:
        with _client() as client:
            _payload, rows = _datacenter_rows(client, "RPTA_WEB_RZRQ_GGMX", _dc_filter("SCOND_CODE", code), 30)
        if not rows:
            return _empty(category, source, url, "未取到融资融券记录", _now(), window)
        return _ok(category, source, url, {"count": len(rows), "rows": _latest_known_dates(rows, ("DATE", "TRADE_DATE"), 30)}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_northbound(code: str, end: date | None = None) -> dict:
    """沪深港通/北向持股，接口可能不公开或字段不同，失败明确标出。"""
    category, source, url = "capital", "eastmoney_northbound", DATACENTER_URL
    window = _window(end)
    reason = ""
    for report_name, field in (
        ("RPT_MUTUAL_STOCK_NORTHSTA", "SECURITY_CODE"),
        ("RPT_MUTUAL_HOLDSTOCKNORTH_STA", "SECURITY_CODE"),
        ("RPTA_WEB_HSGT_HOLD", "SECURITY_CODE"),
    ):
        try:
            with _client() as client:
                _payload, rows = _datacenter_rows(client, report_name, _dc_filter(field, code), 30)
            if rows:
                return _ok(category, source, url, {"count": len(rows), "rows": _latest_known_dates(rows, ("DATE", "TRADE_DATE"), 30)}, _now(), window)
        except Exception as exc:  # noqa: BLE001
            reason = f"{report_name}: {type(exc).__name__}: {exc}"
    return _fail(category, source, url, reason or "未取到北向/沪深港通持股", _now(), window)


def fetch_block_trade(code: str, end: date | None = None) -> dict:
    category, source, url = "trade", "eastmoney_block_trade", DATACENTER_URL
    window = _window(end)
    try:
        with _client() as client:
            _payload, rows = _datacenter_rows(client, "RPT_DATA_BLOCKTRADE", _dc_filter("SECURITY_CODE", code), 20)
        if not rows:
            return _empty(category, source, url, "近 30 天未取到大宗交易", _now(), window)
        return _ok(category, source, url, {"count": len(rows), "rows": _latest_known_dates(rows, ("TRADE_DATE", "DATE"), 20)}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_repurchase(code: str, end: date | None = None) -> dict:
    category, source, url = "trade", "eastmoney_repurchase", DATACENTER_URL
    window = _window(end)
    reason = ""
    for report_name, field in (
        ("RPT_APP_STOCKREPURCHASE", "SECURITY_CODE"),
        ("RPT_SHARE_BONUS_DETAIL", "SECURITY_CODE"),
    ):
        try:
            with _client() as client:
                _payload, rows = _datacenter_rows(client, report_name, _dc_filter(field, code), 20)
            if rows:
                return _ok(category, source, url, {"count": len(rows), "rows": _latest_known_dates(rows, ("DATE", "REPORT_DATE"), 20)}, _now(), window)
        except Exception as exc:  # noqa: BLE001
            reason = f"{report_name}: {type(exc).__name__}: {exc}"
    return _fail(category, source, url, reason or "未取到回购记录", _now(), window)


def fetch_holder_change(code: str, end: date | None = None) -> dict:
    category, source, url = "trade", "eastmoney_holder_change", DATACENTER_URL
    window = _window(end)
    reason = ""
    for report_name, field in (
        ("RPT_EXECUTIVE_HOLD_DETAILS", "SECURITY_CODE"),
        ("RPT_HOLDERS_CHANGE_BOARD", "SECURITY_CODE"),
    ):
        try:
            with _client() as client:
                _payload, rows = _datacenter_rows(client, report_name, _dc_filter(field, code), 20)
            if rows:
                return _ok(category, source, url, {"count": len(rows), "rows": _latest_known_dates(rows, ("DATE", "CHANGE_DATE"), 20)}, _now(), window)
        except Exception as exc:  # noqa: BLE001
            reason = f"{report_name}: {type(exc).__name__}: {exc}"
    return _fail(category, source, url, reason or "未取到增减持记录", _now(), window)


def fetch_unlock(code: str, end: date | None = None) -> dict:
    category, source, url = "trade", "eastmoney_unlock", DATACENTER_URL
    window = _window(end)
    reason = ""
    for report_name, field in (
        ("RPT_LIFT_STOCKDETIAL", "SECURITY_CODE"),
        ("RPT_LIFT_STAGE", "SECURITY_CODE"),
    ):
        try:
            with _client() as client:
                _payload, rows = _datacenter_rows(client, report_name, _dc_filter(field, code), 20)
            if rows:
                return _ok(category, source, url, {"count": len(rows), "rows": _latest_known_dates(rows, ("DATE", "UNFREEZE_DATE"), 20)}, _now(), window)
        except Exception as exc:  # noqa: BLE001
            reason = f"{report_name}: {type(exc).__name__}: {exc}"
    return _fail(category, source, url, reason or "未取到解禁记录", _now(), window)


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
            response = client.get(url, params=params)
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
            response = client.get(url, params={
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
    """公开研报观点摘要。数据源字段不稳定时只保留标题/机构/日期。"""
    category, source, url = "research", "eastmoney_research", DATACENTER_URL
    window = _window(end)
    reason = ""
    for report_name, field in (
        ("RPT_RESEARCH_REPORT", "SECURITY_CODE"),
        ("RPT_RESEARCHREPORT_NEW", "SECURITY_CODE"),
        ("RPT_RESEARCH_REPORT_ORG", "SECURITY_CODE"),
    ):
        try:
            with _client() as client:
                _payload, rows = _datacenter_rows(client, report_name, _dc_filter(field, code), limit)
            if rows:
                digest = []
                for row in rows[:limit]:
                    title = row.get("TITLE") or row.get("TITLE_NAME") or row.get("REPORT_TITLE") or ""
                    org = row.get("ORG_NAME") or row.get("ORG") or row.get("SNAME") or ""
                    pub_date = row.get("PUBLISH_DATE") or row.get("DATE") or row.get("REPORT_DATE") or ""
                    if (title or org) and _date_in_natural_window(pub_date, end)[1]:
                        digest.append({"title": str(title), "org": str(org), "date": _date_str(pub_date)})
                if digest:
                    return _ok(category, source, url, {"count": len(digest), "items": digest}, _now(), window)
        except Exception as exc:  # noqa: BLE001
            reason = f"{report_name}: {type(exc).__name__}: {exc}"
    return _fail(category, source, url, reason or "未取到研报观点", _now(), window)


def fetch_financial_main(code: str, end: date | None = None) -> dict:
    """最新一份定期报告的主要财务指标（报告期、披露日期、营收/净利润/同比等真实字段）。"""
    category, source, url = "financial", "eastmoney_financial_main", F10_MAIN_TARGET_URL
    window = _window(end)
    try:
        with _client() as client:
            response = client.get(url, params={"type": 0, "code": _secucode(code)})
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        payload = response.json()
        rows = payload.get("jbmx") or payload.get("data") or []
        latest = _latest_before_disclosure(rows, end, report_keys=("REPORT_DATE", "DATE"), notice_keys=("NOTICE_DATE", "UPDATE_DATE"))
        if not latest:
            return _empty(category, source, url, "未取到评估日前已披露的定期报告财务数据", _now(), window)
        digest = {
            "report_date": _date_str(latest.get("REPORT_DATE") or latest.get("DATE")),
            "disclosure_date": _date_str(latest.get("NOTICE_DATE") or latest.get("UPDATE_DATE")),
            "revenue": _compact_float(latest.get("TOTALOPERATEREVE")),
            "revenue_yoy": _compact_float(latest.get("TOTALOPERATEREVETZ")),
            "net_profit": _compact_float(latest.get("PARENTNETPROFIT")),
            "net_profit_yoy": _compact_float(latest.get("PARENTNETPROFITTZ")),
            "deducted_profit": _compact_float(latest.get("KCFJCXSYJLR")),
            "eps": _compact_float(latest.get("BASICEPS") or latest.get("EPSJB")),
            "roe": _compact_float(latest.get("ROEJQ") or latest.get("WEIGHTAVG_ROE")),
            "gross_margin": _compact_float(latest.get("XSMLL") or latest.get("GROSS_PROFIT_RATIO")),
            "debt_asset_ratio": _compact_float(latest.get("ZCFZL") or latest.get("DEBT_ASSET_RATIO")),
            "operating_cash_flow": _compact_float(latest.get("JYXJLLJE")),
            "source_note": "字段来自公开 F10 定期报告接口，未取到的字段为 null，不表示 0。",
        }
        return _ok(category, source, url, {"rows_seen": len(rows), "latest": digest}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def fetch_financial_zy(code: str, end: date | None = None) -> dict:
    """最新主要指标接口补充 EPS/ROE/毛利率等，若有则并入证据，失败不算整场失败。"""
    category, source, url = "financial", "eastmoney_financial_zy", F10_ZY_URL
    window = _window(end)
    try:
        with _client() as client:
            response = client.get(url, params={"type": 0, "code": _secucode(code)})
        if response.status_code != 200:
            return _fail(category, source, url, f"HTTP {response.status_code}", _now(), window)
        rows = (response.json() or {}).get("zyzb") or []
        latest = _latest_before_disclosure(rows, end, report_keys=("REPORT_DATE", "date"), notice_keys=("NOTICE_DATE", "UPDATE_DATE"))
        if not latest:
            return _empty(category, source, url, "未取到评估日前已披露的主要财务指标", _now(), window)
        digest = {
            "report_date": _date_str(latest.get("REPORT_DATE") or latest.get("date")),
            "eps": _compact_float(latest.get("EPSJB") or latest.get("BASICEPS")),
            "roe": _compact_float(latest.get("ROEJQ") or latest.get("WEIGHTAVG_ROE")),
            "gross_margin": _compact_float(latest.get("XSMLL") or latest.get("GROSS_PROFIT_RATIO")),
            "net_margin": _compact_float(latest.get("XSJLL") or latest.get("NET_PROFIT_RATIO")),
            "debt_asset_ratio": _compact_float(latest.get("ZCFZL") or latest.get("DEBT_ASSET_RATIO")),
        }
        return _ok(category, source, url, {"latest": digest}, _now(), window)
    except Exception as exc:  # noqa: BLE001
        return _fail(category, source, url, f"{type(exc).__name__}: {exc}", _now(), window)


def collect(code: str, end: date | None = None) -> list[dict]:
    """并发克制地按类别采集；每个源失败只降级。单个源超时秒级，最多 4 个并发。"""
    calls = [
        (lambda c=code: fetch_spot(c, end)),
        (lambda c=code: fetch_sina_spot(c, end)),
        (lambda c=code: fetch_kline(c, end)),
        (lambda c=code: fetch_intraday(c)),
        (lambda c=code: fetch_capital_flow(c, end)),
        (lambda c=code: fetch_lhb(c, end)),
        (lambda c=code: fetch_margin(c, end)),
        (lambda c=code: fetch_northbound(c, end)),
        (lambda c=code: fetch_block_trade(c, end)),
        (lambda c=code: fetch_repurchase(c, end)),
        (lambda c=code: fetch_holder_change(c, end)),
        (lambda c=code: fetch_unlock(c, end)),
        (lambda c=code: fetch_news(c, end)),
        (lambda c=code: fetch_announcements(c, end)),
        (lambda c=code: fetch_research(c, end)),
        (lambda c=code: fetch_financial_main(c, end)),
        (lambda c=code: fetch_financial_zy(c, end)),
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(call) for call in calls]
        return [future.result() for future in futures]
