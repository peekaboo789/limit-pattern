"""腾讯/新浪真实报文解析、板块前缀、自动换源，以及 TXT 导出。"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from backend.app.providers.markets import accept_instrument, classify
from backend.app.providers.router import cross_check
from backend.app.providers.sina import parse_kline as parse_sina_kline
from backend.app.providers.sina import parse_list as parse_sina_list
from backend.app.providers.sina import parse_spot as parse_sina_spot
from backend.app.providers.tencent import parse_kline as parse_qq_kline
from backend.app.providers.tencent import parse_rank, parse_spot as parse_qq_spot
from backend.app.services.limit_up_service import LimitUpDetector, limit_up_price, ratio_for
from backend.app.services.sync_service import run_sync_now, set_fetcher
from tests.test_api import _series, weekdays

# 2026-10-22 从 qt.gtimg.cn 取到的贵州茅台快照，截到涨停价/跌停价。
QQ_SPOT = (
    'v_sh600519="1~贵州茅台~600519~1318.49~1309.69~1303.21~42491~22341~20150~1318.08~1~1318.01~22~1318.00~11~1316.21~2~1316.11~1~1318.49~142~1318.53~184~1318.70~3~1318.80~6~1319.00~58~~20261022161500~8.80~0.67~1322.00~1296.00~1318.49/42491/5576216948~42491~557622~0.34~20.24~~1322.00~1296.00~1.99~16482.20~16482.20~6.56~1440.66~1178.72";'
    'v_sz600000="1~浦发银行~600000~10.00~9.80~9.90~100~0~0";'
    'v_sh000001="1~上证指数~000001~3000~3000~3000~1~0~0";'
)
QQ_KLINE = {
    "code": 0,
    "data": {
        "sh600519": {
            "day": [["2026-10-22", "1303.210", "1318.490", "1322.000", "1296.000", "42491.000"]],
            "qfqday": [["2026-10-22", "1", "1", "1", "1", "1"]],
        }
    },
}
QQ_RANK = {
    "data": {
        "total": 1,
        "rank_list": [
            {
                "code": "sh600519",
                "name": "贵州茅台",
                "volume": "42491.00",
                "turnover": "557622",
                "hsl": "0.34",
                "zxj": "1318.49",
                "zdf": "0.67",
                "stock_type": "GP-A",
            },
            {"code": "bj920000", "name": "安徽凤凰", "volume": "1", "turnover": "1", "hsl": "0.1"},
        ],
    }
}
SINA_SPOT = (
    'var hq_str_sh600519="贵州茅台,1303.210,1309.690,1318.490,1322.000,1296.000,1318.080,1318.490,4249133,5576216948.000,100,1318.080,2026-10-22,15:34:59,00";'
)
SINA_KLINE = [
    {"day": "2026-10-22", "open": "1303.210", "high": "1322.000", "low": "1296.000", "close": "1318.490", "volume": "4249133"}
]
SINA_LIST = [
    {
        "symbol": "bj920000",
        "code": "920000",
        "name": "安徽凤凰",
        "trade": "13.220",
        "volume": 277319,
        "amount": 3676294,
        "turnoverratio": 0.48151,
    },
    {
        "symbol": "sh600519",
        "code": "600519",
        "name": "贵州茅台",
        "trade": "1318.490",
        "settlement": "1309.690",
        "open": "1303.210",
        "high": "1322.000",
        "low": "1296.000",
        "volume": 4249133,
        "amount": 5576216948,
        "turnoverratio": 0.34,
        "changepercent": 0.67,
    },
]


def test_tencent_spot_and_kline_normalize_units():
    spot = {row["code"]: row for row in parse_qq_spot(QQ_SPOT)}
    assert "000001" not in spot
    assert spot["600000"]["code"] == "600000"
    row = spot["600519"]
    assert row["close"] == 1318.49
    assert row["pre_close"] == 1309.69
    assert row["open"] == 1303.21
    assert row["high"] == 1322
    assert row["low"] == 1296
    assert row["volume"] == 4249100
    assert row["amount"] == 5576216948
    assert row["turnover_rate"] == 0.34
    assert row["limit_up_price"] == 1440.66
    bars = parse_qq_kline(QQ_KLINE)
    assert len(bars) == 1
    assert bars[0]["volume"] == 4249100
    assert bars[0]["close"] == 1318.49
    ranked = {item["code"]: item for item in parse_rank(QQ_RANK)}
    assert "920000" not in ranked
    assert ranked["600519"]["volume"] == 4249100
    assert ranked["600519"]["amount"] == 557622 * 10000
    assert ranked["600519"]["turnover_rate"] == 0.34


def test_sina_spot_kline_and_list_keep_share_units():
    spot = parse_sina_spot(SINA_SPOT)[0]
    assert spot["code"] == "600519"
    assert spot["volume"] == 4249133
    assert spot["amount"] == 5576216948
    assert spot["close"] == 1318.49
    assert spot["pre_close"] == 1309.69
    bars = parse_sina_kline(SINA_KLINE)
    assert bars[0]["volume"] == 4249133
    listed = {item["code"]: item for item in parse_sina_list(SINA_LIST)}
    assert "920000" not in listed
    assert listed["600519"]["volume"] == 4249133
    assert listed["600519"]["amount"] == 5576216948
    assert listed["600519"]["turnover_rate"] == 0.34


def test_mismatched_market_prefix_follows_the_code():
    kept = accept_instrument("sz600519", "600519", "贵州茅台")
    assert kept == ("600519", "SSE")
    assert accept_instrument("sh000001", "000001", "上证指数") is None
    assert accept_instrument("sz000001", "000001", "平安银行") == ("000001", "SZSE")
    assert accept_instrument("sh900901", "900901", "沪B") is None
    assert accept_instrument("sz200011", "200011", "深B") is None


@pytest.mark.parametrize(
    ("code", "exchange", "board", "ratio"),
    [
        ("600000", "SSE", "main_board", "0.10"),
        ("601398", "SSE", "main_board", "0.10"),
        ("603259", "SSE", "main_board", "0.10"),
        ("605117", "SSE", "main_board", "0.10"),
        ("688981", "SSE", "star", "0.20"),
        ("689009", "SSE", "star", "0.20"),
        ("000001", "SZSE", "main_board", "0.10"),
        ("001979", "SZSE", "main_board", "0.10"),
        ("002594", "SZSE", "main_board", "0.10"),
        ("003816", "SZSE", "main_board", "0.10"),
        ("300750", "SZSE", "chinext", "0.20"),
        ("301236", "SZSE", "chinext", "0.20"),
        ("302132", "SZSE", "chinext", "0.20"),
    ],
)
def test_each_prefix_board_and_limit_ratio(code, exchange, board, ratio):
    assert classify(code) == (exchange, board)
    assert ratio_for(board, False, date(2026, 8, 3)) == Decimal(ratio)


@pytest.mark.parametrize("code", ["900901", "200011", "920001", "430047", "830001", "510300", "159915", "399006", "110059"])
def test_excluded_prefixes(code):
    exchange, _board = classify(code)
    assert exchange not in {"SSE", "SZSE"}


def test_limit_price_rounding_and_special_cases():
    assert float(limit_up_price(10.05, Decimal("0.10"))) == 11.06
    assert float(limit_up_price(10, Decimal("0.20"))) == 12.0
    assert float(limit_up_price(3.19, ratio_for("main_board", False, date(2026, 9, 1)))) == 3.51
    detector = LimitUpDetector()
    chinext = detector.label_bar(
        board="chinext",
        is_st=True,
        trade_date=date(2026, 6, 1),
        open_=10,
        high=12,
        low=10,
        close=12,
        pre_close=10,
        volume=100,
        list_date=date(2020, 1, 2),
        trading_days=_calendar(date(2020, 1, 2), date(2026, 6, 2)),
    )
    assert chinext.limit_up_percent == 20
    assert chinext.is_limit_up
    listing = date(2024, 6, 3)
    calendar = _calendar(listing, date(2024, 6, 20))
    fifth = [day for day in calendar if day >= listing][4]
    fresh = detector.label_bar(
        board="main_board",
        is_st=False,
        trade_date=fifth,
        open_=10,
        high=12,
        low=10,
        close=12,
        pre_close=10,
        volume=100,
        list_date=listing,
        trading_days=calendar,
    )
    assert fresh.reason == "ipo_no_limit"
    old_listing = date(2019, 6, 3)
    old_calendar = _calendar(old_listing, date(2019, 6, 20))
    old_fifth = [day for day in old_calendar if day >= old_listing][4]
    aged = detector.label_bar(
        board="main_board",
        is_st=False,
        trade_date=old_fifth,
        open_=10,
        high=11,
        low=10,
        close=11,
        pre_close=10,
        volume=100,
        list_date=old_listing,
        trading_days=old_calendar,
    )
    assert aged.is_limit_up
    assert aged.limit_up_percent == 10
    gem = detector.label_bar(
        board="chinext",
        is_st=False,
        trade_date=date(2026, 8, 3),
        open_=10,
        high=12,
        low=10,
        close=12,
        pre_close=10,
        volume=100,
        list_date=None,
        trading_days=[],
    )
    assert gem.is_limit_up and gem.limit_up_percent == 20


def test_auto_source_falls_back_and_cross_check_flags_mismatch(client, monkeypatch):
    set_fetcher()
    days = weekdays(4)

    def boom(*_args, **_kwargs):
        from backend.app.providers.eastmoney import ProviderError

        raise ProviderError("超时")

    monkeypatch.setattr("backend.app.providers.eastmoney.fetch_stock_list", boom)
    monkeypatch.setattr("backend.app.providers.eastmoney.fetch_kline", boom)
    monkeypatch.setattr("backend.app.providers.tencent.fetch_stock_list", lambda: [("600000", "浦发银行")])
    monkeypatch.setattr(
        "backend.app.providers.tencent.fetch_kline",
        lambda code, start, end: _series(days, limit_on={3}, start=12),
    )
    monkeypatch.setattr(
        "backend.app.providers.eastmoney.fetch_spot",
        lambda codes: [{"code": "600000", "close": 1.0, "pre_close": 1.0, "limit_up_price": 9.9}],
    )
    notes = cross_check("tencent", [{"code": "600000", "close": 13.6, "pre_close": 12.36}])
    assert any("不一致" in note for note in notes)
    client.put("/api/settings", json={"data_source": "auto"})
    result = run_sync_now()
    assert result["active_source"] == "tencent"
    assert result["data_source"] == "auto"
    health = client.get("/api/health").json()
    assert health["active_source"] == "tencent"
    assert health["exports_dir"].endswith("exports")


def test_export_txt_has_bom_table_and_copy(client):
    from tests.test_api import _save

    days = weekdays(8)
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={5, 6, 7}))
    _save("000001", "平安银行", "SZSE", _series(days, limit_on=set(), start=8))
    last = days[-1].isoformat()
    response = client.get("/api/export", params={"type": "scan", "date": last, "top": 10})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    disposition = response.headers["content-disposition"]
    assert f'filename="scan_{last}.txt"' in disposition
    assert "filename*=UTF-8''" in disposition
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8-sig")
    assert "相似度不是涨停概率" in text
    assert "排名\t代码\t名称\t总分\t形态分\t量能分\t量比分\t最相似涨停股\t涨停日期" in text
    assert "000001" in text
    from backend.app.core.config import exports_dir

    saved = exports_dir() / f"scan_{last}.txt"
    assert saved.read_bytes().startswith(b"\xef\xbb\xbf")
    single = client.get("/api/export", params={"type": "similarity", "date": last, "code": "000001"})
    assert single.status_code == 200
    assert f"similarity_{last}_000001.txt" in single.headers["content-disposition"]
    missing = client.get("/api/export", params={"type": "similarity", "date": last})
    assert missing.status_code == 422
    bad = client.get("/api/export", params={"type": "nope", "date": last})
    assert bad.status_code == 422
    client.put(
        "/api/settings",
        json={"ai_base_url": "", "ai_model": "", "data_source": "eastmoney"},
    )
    made = client.post(f"/api/summaries/{last}/generate")
    assert made.status_code == 200
    summary = client.get("/api/export", params={"type": "summary", "date": last})
    assert summary.status_code == 200
    summary_text = summary.content.decode("utf-8-sig")
    assert "原始JSON" in summary_text
    assert '"source": "statistics"' in summary_text or '"source":"statistics"' in summary_text
    assert "板块" in summary_text


def _calendar(start: date, end: date) -> list[date]:
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days
