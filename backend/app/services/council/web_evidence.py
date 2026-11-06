"""多模型讨论的联网核对：只做可验证的公开行情/新闻/公告快照，逐源降级，不把 HTML 整页塞进提示词。

所有结果都是结构化摘要，字段含 source/url/fetched_at/status/digest；拿不到就写失败原因。
估值、市盈率、盈利、股东、北向资金等输入里没有的数据严禁从接口里推出来。
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx

USER_AGENT = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
PUSH2_URL = "https://push2.eastmoney.com/api/qt/stock/get"
NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def _client():
    # 每个调用方用自己的短连接，避免线程间复用复杂化；连接/读超时都收紧到秒级。
    return httpx.Client(timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0), headers=USER_AGENT, follow_redirects=True)


def _market(code: str) -> str:
    return "1" if code.startswith(("6", "9")) else "0"


def _ok(status: str, source: str, url: str, digest: dict | list | str | None, fetched_at: str) -> dict:
    return {"status": status, "source": source, "url": url, "fetched_at": fetched_at, "digest": digest}


def _fail(source: str, url: str, reason: str, fetched_at: str) -> dict:
    return {"status": "failed", "source": source, "url": url, "fetched_at": fetched_at, "digest": {"reason": reason[:240]}}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def fetch_spot(code: str) -> dict:
    """公开行情快照：与本地最近收盘/涨跌核对用。"""
    url = PUSH2_URL
    try:
        with _client() as client:
            response = client.get(url, params={
                "secid": f"{_market(code)}.{code}",
                "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170",
                "fltt": 2,
                "invt": 2,
            })
        if response.status_code != 200:
            return _fail("eastmoney_spot", url, f"HTTP {response.status_code}", _now())
        data = (response.json() or {}).get("data")
        if not data:
            return _fail("eastmoney_spot", url, "接口没有返回快照", _now())
        digest = {
            "code": str(data.get("f57") or code),
            "name": data.get("f58"),
            "price": data.get("f43"),
            "pct_chg": data.get("f170"),
            "high": data.get("f44"),
            "low": data.get("f45"),
            "open": data.get("f46"),
            "pre_close": data.get("f60"),
        }
        return _ok("ok", "eastmoney_spot", url, digest, _now())
    except Exception as exc:  # noqa: BLE001 - 单源失败不能带崩整场
        return _fail("eastmoney_spot", url, f"{type(exc).__name__}: {exc}", _now())


def fetch_news(code: str, limit: int = 6) -> dict:
    """最近公开新闻/快讯标题。只保留标题、时间、来源、链接，当公开表述证据。"""
    url = NEWS_URL
    keyword = code
    try:
        params = {
            "cb": "jQuery",
            "param": '{"uid":"","keyword":"%s","type":["cmsArticleWebOld"],"client":"web","clientType":"web","clientVersion":"curr","param":{"cmsArticleWebOld":{"searchScope":"default","sort":"default","pageIndex":1,"pageSize":%d,"preTag":"","postTag":""}}}' % (keyword, limit),
        }
        with _client() as client:
            response = client.get(url, params=params)
        text = response.text or ""
        if response.status_code != 200 or "jQuery(" not in text:
            return _fail("eastmoney_news", url, f"HTTP {response.status_code}" if response.status_code != 200 else "接口返回格式异常", _now())
        import json as _json

        payload = _json.loads(text[text.find("(") + 1 : text.rfind(")")])
        rows = ((((payload or {}).get("result") or {}).get("cmsArticleWebOld") or [])[:limit])
        digest = [
            {
                "title": str(row.get("title") or row.get("Title") or "").strip(),
                "date": str(row.get("date") or row.get("Date") or ""),
                "source": str(row.get("mediaName") or row.get("source") or ""),
                "url": str(row.get("url") or row.get("Url") or ""),
            }
            for row in rows
        ]
        digest = [row for row in digest if row["title"] or row["url"]]
        return _ok("ok", "eastmoney_news", url, digest, _now())
    except Exception as exc:  # noqa: BLE001
        return _fail("eastmoney_news", url, f"{type(exc).__name__}: {exc}", _now())


def fetch_announcements(code: str) -> dict:
    """公开公告标题。接口不稳定时明确返回“未取到公告”。"""
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    try:
        with _client() as client:
            response = client.get(url, params={
                "sr": "-1",
                "page_size": 5,
                "page_index": 1,
                "ann_type": "A",
                "client_source": "web",
                "stock_list": code,
            })
        if response.status_code != 200:
            return _fail("eastmoney_announcements", url, f"HTTP {response.status_code}", _now())
        data = (response.json() or {}).get("data") or {}
        rows = data.get("list") or []
        digest = [
            {
                "title": str(row.get("title") or "").strip(),
                "notice_date": str(row.get("notice_date") or "").split(" ")[0],
                "url": str(row.get("art_code") or ""),
            }
            for row in rows
        ]
        digest = [row for row in digest if row["title"]]
        return _ok("ok" if digest else "empty", "eastmoney_announcements", url, digest or "未取到公告", _now())
    except Exception as exc:  # noqa: BLE001
        return _fail("eastmoney_announcements", url, f"{type(exc).__name__}: {exc}", _now())


def collect(code: str) -> list[dict]:
    return [fetch_spot(code), fetch_news(code), fetch_announcements(code)]
