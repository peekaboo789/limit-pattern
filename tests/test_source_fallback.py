"""逐只换源，以及新浪缺成交额时的补齐和估算。"""

from datetime import date

from backend.app.providers.eastmoney import ProviderError
from backend.app.services import sync_service as sync


def _bar(**extra):
    payload = {
        "trade_date": date(2026, 10, 22),
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10,
        "volume": 1000,
        "amount": None,
        "turnover_rate": None,
    }
    payload.update(extra)
    return payload


def test_outage_switches_the_rest_of_the_chain(monkeypatch):
    sync._clear_source_health()
    calls: list[str] = []

    def kline_of(name):
        def fetch(code, start, end):
            calls.append(f"{name}:{code}")
            if name == "tencent":
                raise ProviderError("腾讯日线请求失败（HTTP 501）")
            return [_bar(amount=5000, turnover_rate=1.2)]

        return fetch

    monkeypatch.setattr(sync, "_kline_of", kline_of)
    monkeypatch.setattr(sync, "_pace", lambda: None)
    chain = ["tencent", "sina"]
    source, bars = sync.fetch_from_chain("000001", date(2026, 10, 1), date(2026, 10, 22), chain)
    assert source == "sina"
    assert bars[0]["amount"] == 5000
    source, _bars = sync.fetch_from_chain("000002", date(2026, 10, 1), date(2026, 10, 22), chain)
    assert source == "sina"
    assert calls == ["tencent:000001", "sina:000001", "sina:000002"]
    sync._clear_source_health()


def test_other_source_fills_amount_and_turnover(monkeypatch):
    sync._clear_source_health()

    def kline_of(name):
        def fetch(code, start, end):
            if name == "eastmoney":
                return [_bar(amount=22222, turnover_rate=3.5)]
            raise ProviderError("不该再问新浪")

        return fetch

    monkeypatch.setattr(sync, "_kline_of", kline_of)
    monkeypatch.setattr(sync, "_pace", lambda: None)
    filled = sync.fill_missing_fields("000001", [_bar()], "sina", ["sina", "eastmoney"])
    assert filled[0]["amount"] == 22222
    assert filled[0]["turnover_rate"] == 3.5
    assert filled[0]["amount_estimated"] is False
    assert filled[0]["source"] == "sina"
    sync._clear_source_health()


def test_amount_is_estimated_from_volume_and_marked(monkeypatch):
    sync._clear_source_health()
    monkeypatch.setattr(sync, "_pace", lambda: None)
    filled = sync.fill_missing_fields("000001", [_bar()], "sina", ["sina"])
    assert filled[0]["amount"] == 10000
    assert filled[0]["amount_estimated"] is True
    assert filled[0]["turnover_rate"] is None
    sync._clear_source_health()
