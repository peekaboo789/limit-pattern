from backend.app.providers.eastmoney import parse_kline, parse_stock_list


def test_stock_list_keeps_only_sh_sz_a():
    payload = {
        "data": {
            "diff": [
                {"f12": "600000", "f14": "浦发银行"},
                {"f12": "000001", "f14": "平安银行"},
                {"f12": "920001", "f14": "北交"},
                {"f12": "510300", "f14": "沪深300ETF"},
            ],
            "total": 4,
        }
    }
    codes = [code for code, _name in parse_stock_list(payload)]
    assert codes == ["600000", "000001"]


def test_kline_parser_uses_unadjusted_fields():
    payload = {
        "data": {
            "klines": [
                "2026-06-01,10.00,11.00,11.00,9.80,1200,340000,2.0,10.0,1.00,1.5",
            ]
        }
    }
    bars = parse_kline(payload)
    assert bars[0]["trade_date"].isoformat() == "2026-06-01"
    assert bars[0]["open"] == 10
    assert bars[0]["close"] == 11
    assert bars[0]["high"] == 11
    assert bars[0]["low"] == 9.8
    assert bars[0]["pre_close"] == 10
    assert bars[0]["volume"] == 1200
    assert bars[0]["amount"] == 340000
    assert bars[0]["turnover_rate"] == 1.5
