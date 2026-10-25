"""接口契约、三日样本池、自身排除、不看未来、明细清理、总结永久保留。"""

import json
from datetime import date, timedelta

from backend.app.core.database import session_scope
from backend.app.models.tables import AiDailySummary, CalcBar, DailyQuote, Stock, TradingDay
from backend.app.providers.markets import keep_code
from backend.app.services.limit_up_service import annotate_history, ratio_for
from backend.app.services.limit_up_service import limit_up_price
from backend.app.services.retention import purge_expired
from backend.app.services.sync_service import run_sync_now, set_fetcher


def weekdays(count: int, end: date = date(2026, 6, 12)) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _series(days: list[date], *, limit_on: set[int], start: float = 10.0) -> list[dict]:
    price = start
    rows = []
    for index, day in enumerate(days):
        pre = round(price, 2)
        if index in limit_on:
            close = float(limit_up_price(pre, ratio_for("main_board", False, day)))
            high = close
            low = pre
        else:
            close = round(pre * 1.01, 2)
            high = close
            low = pre
        rows.append(
            {
                "trade_date": day,
                "open": pre,
                "high": high,
                "low": low,
                "close": close,
                "pre_close": pre,
                "volume": 1000 + index * 100,
                "amount": 10000 + index,
                "turnover_rate": 1.2 + index / 10,
            }
        )
        price = close
    return rows


def _save(code: str, name: str, exchange: str, rows: list[dict]) -> None:
    board = "main_board"
    labelled = annotate_history(rows, board=board, is_st=False, list_date=None, trading_days=[row["trade_date"] for row in rows])
    with session_scope() as session:
        stock = session.get(Stock, code)
        if stock is None:
            stock = Stock(code=code)
            session.add(stock)
        stock.name = name
        stock.exchange = exchange
        stock.board = board
        for item in labelled:
            session.add(
                DailyQuote(
                    stock_code=code,
                    trade_date=item["trade_date"],
                    open=item["open"],
                    high=item["high"],
                    low=item["low"],
                    close=item["close"],
                    pre_close=item["pre_close"],
                    volume=item["volume"],
                    amount=item["amount"],
                    turnover_rate=item["turnover_rate"],
                    change_percent=item.get("change_percent"),
                    is_limit_up=item["is_limit_up"],
                    is_limit_down=item["is_limit_down"],
                    limit_up_price=item["limit_up_price"],
                    limit_down_price=item["limit_down_price"],
                    limit_up_percent=item["limit_up_percent"],
                    limit_up_reason=item["limit_up_reason"],
                    source="test",
                )
            )
            if session.get(TradingDay, item["trade_date"]) is None:
                session.add(TradingDay(trade_date=item["trade_date"], is_open=True, limit_up_count=0))


def _seed() -> list[date]:
    days = weekdays(8)
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={5, 6, 7}))
    _save("000001", "平安银行", "SZSE", _series(days, limit_on=set(), start=8.0))
    return days


def test_health_and_settings_round_trip(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["data_date"] is None
    secret = "sk-test-secret-123456"
    saved = client.put(
        "/api/settings",
        json={
            "retention_trading_days": 6,
            "ai_base_url": "https://example.invalid/v1",
            "ai_model": "demo-model",
            "ai_api_key": secret,
            "data_source": "eastmoney",
        },
    )
    assert saved.status_code == 200
    view = saved.json()
    assert view["retention_trading_days"] == 6
    assert view["compare_window_trading_days"] == 3
    assert view["ai_enabled"] is True
    assert view["ai_api_key_set"] is True
    assert secret not in saved.text
    assert "ai_api_key" not in view
    again = client.get("/api/settings")
    assert secret not in again.text
    assert again.json()["ai_model"] == "demo-model"
    too_small = client.put("/api/settings", json={"retention_trading_days": 2})
    assert too_small.status_code == 422


def test_compare_window_is_customisable_within_retention(client):
    settings = client.get("/api/settings").json()
    assert settings["compare_window_trading_days"] == 3
    assert settings["compare_window_min"] == 1
    retention = settings["retention_trading_days"]

    accepted = client.put("/api/settings", json={"compare_window_trading_days": 4})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["compare_window_trading_days"] == 4
    assert client.get("/api/settings").json()["compare_window_trading_days"] == 4

    # 窗口比保留天数还长，样本池里那几天的明细早被清掉了：拒收，而不是悄悄截短。
    for bad in (retention + 1, 0, -2, 3.5, "abc", True):
        response = client.put("/api/settings", json={"compare_window_trading_days": bad})
        assert response.status_code == 422, (bad, response.text)
    assert client.get("/api/settings").json()["compare_window_trading_days"] == 4, "拒收的值把旧设置改掉了"

    # 保留天数砍到比窗口还短，同样拒收；两项一起改、一起合法才收。
    shrink = client.put("/api/settings", json={"retention_trading_days": 3})
    assert shrink.status_code == 422, shrink.text
    both = client.put("/api/settings", json={"retention_trading_days": 3, "compare_window_trading_days": 2})
    assert both.status_code == 200, both.text
    assert both.json()["retention_trading_days"] == 3
    assert both.json()["compare_window_trading_days"] == 2


def test_pool_follows_the_compare_window_setting(client):
    days = _seed()
    last = days[-1].isoformat()
    for window in (3, 2, 1):
        assert client.put("/api/settings", json={"compare_window_trading_days": window}).status_code == 200
        pool = client.get("/api/pool").json()
        assert pool["dates"] == [day.isoformat() for day in days[-window:]], window
        scan = client.get("/api/scan", params={"date": last, "top": 20}).json()
        assert scan["pool_size"] == window, (window, scan["pool_size"])
        status = client.get("/api/data/status").json()
        assert status["compare_window_days"] == window


def test_pool_is_three_days_and_excludes_self_and_future(client):
    days = _seed()
    last = days[-1].isoformat()
    pool = client.get("/api/pool").json()
    assert pool["dates"] == [day.isoformat() for day in days[-3:]]
    assert pool["items"]
    assert {item["code"] for item in pool["items"]} == {"600000"}
    assert {item["board"] for item in pool["items"]} == {"SH"}
    scan = client.get("/api/scan", params={"date": last, "top": 20}).json()
    assert scan["disclaimer"] == "相似度不是涨停概率"
    assert scan["pool_size"] == 3
    codes = {item["code"] for item in scan["items"]}
    assert "000001" in codes
    for item in scan["items"]:
        assert item["best_match"]["code"] != item["code"]
        assert 0 <= item["score"] <= 100
        assert "shape_score" in item and "volume_score" in item and "volume_pct_score" in item
    own = client.get("/api/similarity", params={"code": "600000", "date": last}).json()
    assert own["pool_dates"] == pool["dates"]
    assert all(match["code"] != "600000" for match in own["matches"])
    other = client.get("/api/similarity", params={"code": "000001", "date": last, "top": 5}).json()
    assert other["disclaimer"] == "相似度不是涨停概率"
    assert other["matches"]
    assert other["matches"][0]["code"] == "600000"
    assert len(other["target"]["window"]) == 3
    assert other["target"]["window"][-1] == last
    assert other["matches"][0]["window"][-1] < other["matches"][0]["limit_date"]
    before = other["matches"]
    with session_scope() as session:
        session.add(
            DailyQuote(
                stock_code="000001",
                trade_date=days[-1] + timedelta(days=1),
                open=1,
                high=99,
                low=1,
                close=99,
                pre_close=1,
                volume=9,
                is_limit_up=True,
                is_limit_down=False,
                source="test",
            )
        )
    after = client.get("/api/similarity", params={"code": "000001", "date": last, "top": 5}).json()
    assert after["matches"] == before
    future = client.get("/api/scan", params={"date": "2099-01-04"})
    assert future.status_code == 400
    assert client.get("/api/similarity", params={"code": "920001"}).status_code == 422


def test_retention_keeps_summaries(client, monkeypatch):
    days = _seed()
    monkeypatch.setattr(
        "backend.app.services.summary_service.call_chat",
        lambda _prompt: json.dumps({"headline": "周一总结", "key_points": ["只看行情", "没有预测"]}),
    )
    client.put(
        "/api/settings",
        json={"ai_base_url": "https://example.invalid/v1", "ai_model": "demo", "ai_api_key": "sk-test-secret-123456"},
    )
    created = client.post(f"/api/summaries/{days[0].isoformat()}/generate")
    assert created.status_code == 200, created.text
    payload = created.json()
    for key in ("date", "limitup_count", "sectors", "ladder", "patterns", "volume_features", "key_points", "model", "generated_at"):
        assert key in payload
    assert "sk-test-secret-123456" not in created.text
    listed = client.get("/api/summaries").json()["items"]
    assert listed[0]["date"] == days[0].isoformat()
    assert listed[0]["headline"] == "周一总结"
    result = purge_expired()
    assert result["deleted"]["daily_quotes"] > 0
    assert len(result["kept_dates"]) == 5
    assert days[0].isoformat() not in result["kept_dates"]
    kept = client.get("/api/summaries").json()["items"]
    assert kept[0]["date"] == days[0].isoformat()
    detail = client.get(f"/api/summaries/{days[0].isoformat()}")
    assert detail.status_code == 200
    from backend.app.core.config import summaries_dir

    exported = summaries_dir() / f"{days[0].isoformat()}.json"
    assert json.loads(exported.read_text(encoding="utf-8"))["headline"] == "周一总结"
    with session_scope() as session:
        assert session.get(AiDailySummary, days[0]) is not None
        assert session.query(DailyQuote).filter(DailyQuote.trade_date == days[0]).count() == 0
    settings = client.get("/api/settings").json()
    assert settings["last_cleanup_at"]
    blocked = client.post(f"/api/summaries/{days[-1].isoformat()}/generate")
    # 密钥还在，这一天应该能生成。再测未配置。
    assert blocked.status_code == 200
    fresh = client.put("/api/settings", json={"ai_base_url": "", "ai_model": ""})
    assert fresh.json()["ai_enabled"] is False
    stats = client.post(f"/api/summaries/{days[-1].isoformat()}/generate")
    assert stats.status_code == 200
    body = stats.json()
    assert body["source"] == "statistics"
    assert body["model"] == ""
    assert body["limitup_count"] >= 1
    assert body["sectors"] and body["ladder"] and body["volume_features"]
    assert any(point.startswith("板块：") for point in body["key_points"])


def test_sync_filters_non_ashares(client):
    days = weekdays(4)

    def list_fn():
        return [("600519", "贵州茅台"), ("920001", "北交所")]

    def kline_fn(code, start, end):
        if code != "600519":
            return _series(days, limit_on={3})
        return _series(days, limit_on={3}, start=20)

    set_fetcher(list_fn, kline_fn)
    result = run_sync_now()
    assert result["running"] is False
    assert result["progress"] == 100
    health = client.get("/api/health").json()
    assert health["data_date"] == days[-1].isoformat()
    codes = {item["code"] for item in client.get("/api/limitups").json()["items"]}
    assert "920001" not in codes
    assert "600519" in codes
    turned = client.put("/api/settings", json={"data_source": "sina"})
    assert turned.status_code == 200
    again = run_sync_now()
    assert again["progress"] == 100
    assert again["active_source"] == "sina"
    assert client.get("/api/health").json()["active_source"] == "sina"
    rejected = client.put("/api/settings", json={"data_source": "foo"})
    assert rejected.status_code == 422


def test_star_board_is_excluded_from_queries_and_cleanup(client, monkeypatch):
    denied = client.get("/api/kline", params={"code": "688981"})
    assert denied.status_code == 422
    assert denied.json()["detail"] == "科创板不在纳入范围"
    denied = client.get("/api/similarity", params={"code": "689009"})
    assert denied.status_code == 422
    assert denied.json()["detail"] == "科创板不在纳入范围"
    days = _seed()
    with session_scope() as session:
        session.add(Stock(code="688981", name="中芯国际", exchange="SSE", board="star"))
        session.add(
            DailyQuote(
                stock_code="688981",
                trade_date=days[-1],
                open=10,
                high=12,
                low=10,
                close=12,
                pre_close=10,
                volume=1000,
                amount=1000,
                is_limit_up=True,
                source="test",
            )
        )
    listed = {item["code"] for item in client.get("/api/limitups").json()["items"]}
    assert "688981" not in listed
    from backend.app.services.retention import purge_excluded_boards

    removed = purge_excluded_boards()
    assert removed["codes"] == 1
    with session_scope() as session:
        assert session.get(Stock, "688981") is None
        left = session.query(DailyQuote).filter(DailyQuote.stock_code == "688981").count()
    assert left == 0
    view = client.get("/api/settings").json()
    assert view["excluded_boards"] == ["star"]
    assert "创业板（300/301/302）" in view["market_scope"]
    assert "688/689" not in view["market_scope"]
    monkeypatch.setenv("EXCLUDED_BOARDS", "")
    assert keep_code("688981")


def test_market_prefixes():
    assert keep_code("600000")
    assert keep_code("000001")
    assert keep_code("300750")
    assert not keep_code("688981")
    assert keep_code("302132")
    assert not keep_code("920001")
    assert not keep_code("200001")
    assert not keep_code("900901")
    assert not keep_code("510300")
    assert not keep_code("399006")


def test_kline_does_not_return_future_bars(client):
    days = _seed()
    with session_scope() as session:
        session.add(
            DailyQuote(
                stock_code="000001",
                trade_date=date(2099, 1, 4),
                open=1,
                high=2,
                low=1,
                close=2,
                volume=10,
                source="test",
            )
        )
    anchor_day = ""
    anchor_close = None
    with session_scope() as session:
        official = (
            session.query(DailyQuote)
            .filter(DailyQuote.stock_code == "000001")
            .order_by(DailyQuote.trade_date)
            .first()
        )
        assert official is not None
        anchor_day = official.trade_date.isoformat()
        anchor_close = official.close
        session.add(
            CalcBar(
                stock_code="000001",
                trade_date=date(2026, 5, 20),
                open=7.5,
                high=7.8,
                low=7.4,
                close=7.7,
                pre_close=7.5,
                volume=800,
                amount=6000,
            )
        )
        session.add(
            CalcBar(
                stock_code="000001",
                trade_date=official.trade_date,
                open=1,
                high=1,
                low=1,
                close=1,
                pre_close=1,
                volume=1,
            )
        )
    bars = client.get("/api/kline", params={"code": "000001", "days": 30}).json()["bars"]
    assert bars
    assert all(bar["date"] <= "2026-10-22" for bar in bars)
    assert all(bar["date"] != "2099-01-04" for bar in bars)
    assert bars[0]["date"] == "2026-05-20"
    assert bars[0]["pct_chg"] == 2.6667
    same_day = next(bar for bar in bars if bar["date"] == anchor_day)
    assert same_day["close"] == anchor_close
