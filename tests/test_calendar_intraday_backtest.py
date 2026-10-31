"""交易日历、盘中临时日线、11:30 冻结、回测不看未来、次日观察池。"""

import json
from datetime import date, datetime

from backend.app.core.calendar import is_trading_day, market_open, next_trading_day, previous_trading_day
from backend.app.core.clock import SHANGHAI
from backend.app.core.database import session_scope
from backend.app.models.tables import AiRanking, DailyQuote
from backend.app.services import intraday_service
from backend.app.services.backtest_service import band_of, ranking_backtest, similarity_backtest
from backend.app.services.ranking_service import generate_ranking, previous_ranking
from tests.test_ranking_monitor import _fake_chat, _seed_many


def test_calendar_holidays_and_neighbours():
    assert not is_trading_day(date(2026, 10, 1))
    assert not is_trading_day(date(2026, 10, 3))
    assert is_trading_day(date(2026, 10, 8))
    assert previous_trading_day(date(2026, 10, 8)) == date(2026, 9, 30)
    assert next_trading_day(date(2026, 9, 30)) == date(2026, 10, 8)
    assert previous_trading_day(date(2025, 2, 5)) == date(2025, 1, 27)
    assert not market_open(datetime(2026, 10, 5, 10, 0, tzinfo=SHANGHAI))
    assert market_open(datetime(2026, 10, 9, 10, 0, tzinfo=SHANGHAI))
    assert not market_open(datetime(2026, 10, 9, 12, 0, tzinfo=SHANGHAI))


def test_holiday_override_file(client, tmp_path):
    (tmp_path / "holidays.json").write_text(json.dumps({"closed": ["2026-11-02"]}), encoding="utf-8")
    assert not is_trading_day(date(2026, 11, 2))
    info = client.get("/api/calendar", params={"year": 2027}).json()
    assert info["estimated"] is True


def _spot(day):
    def fetch(codes):
        rows = []
        for code in codes:
            rows.append({"code": code, "trade_date": day, "open": 10.0, "high": 10.6, "low": 9.9, "close": 10.5, "pre_close": 10.0, "volume": 5000, "amount": 50000, "turnover_rate": 1.0})
        return rows
    return fetch


def test_ranking_uses_intraday_snapshot_then_cleans_up(client):
    days = _seed_many()
    today = date(2026, 6, 15)
    intraday_service.set_bulk_fetcher(_spot(today))
    try:
        with session_scope() as session:
            payload = generate_ranking(session, chat=_fake_chat, moment=datetime(2026, 6, 15, 10, 30, tzinfo=SHANGHAI))
            assert payload["date"] == today.isoformat()
            assert payload["snapshot"]["kind"] == "intraday"
            assert payload["snapshot"]["count"] > 0
            assert today.isoformat() in payload["pool_dates"]
            assert session.query(DailyQuote).filter(DailyQuote.trade_date == today).count() == 0
            # 收盘后到同步前：用收盘报价
            closed = generate_ranking(session, chat=_fake_chat, moment=datetime(2026, 6, 15, 15, 20, tzinfo=SHANGHAI))
            assert closed["snapshot"]["kind"] == "close"
            # 休市日不用快照
            holiday = generate_ranking(session, chat=_fake_chat, moment=datetime(2026, 6, 19, 10, 0, tzinfo=SHANGHAI))
            assert holiday["snapshot"]["kind"] == "daily"
            assert holiday["date"] == days[-1].isoformat()
    finally:
        intraday_service.set_bulk_fetcher()


def test_stale_quotes_are_not_treated_as_today(client):
    _seed_many()
    intraday_service.set_bulk_fetcher(_spot(date(2026, 6, 12)))
    try:
        with session_scope() as session:
            payload = generate_ranking(session, chat=_fake_chat, moment=datetime(2026, 6, 15, 10, 30, tzinfo=SHANGHAI))
        assert payload["snapshot"]["kind"] == "daily"
    finally:
        intraday_service.set_bulk_fetcher()


def test_morning_freeze_is_used_in_the_afternoon(client):
    _seed_many()
    today = date(2026, 6, 15)
    intraday_service.set_bulk_fetcher(_spot(today))
    try:
        saved = intraday_service.save_morning_freeze(datetime(2026, 6, 15, 11, 30, 20, tzinfo=SHANGHAI))
        assert saved and saved["count"] > 0
        with session_scope() as session:
            lunch = generate_ranking(session, chat=_fake_chat, moment=datetime(2026, 6, 15, 12, 0, tzinfo=SHANGHAI))
            assert lunch["snapshot"]["kind"] == "morning_freeze"
            afternoon = generate_ranking(session, chat=_fake_chat, moment=datetime(2026, 6, 15, 14, 0, tzinfo=SHANGHAI), prefer="morning")
            assert afternoon["snapshot"]["kind"] == "morning_freeze"
            assert afternoon["snapshot"]["taken_at"].startswith("2026-06-15T11:30")
    finally:
        intraday_service.set_bulk_fetcher()


def test_previous_ranking_follows_calendar(client):
    _seed_many()
    with session_scope() as session:
        for day in (date(2026, 9, 29), date(2026, 9, 30)):
            session.add(AiRanking(trade_date=day, run_at="x", trigger="t", payload={"date": day.isoformat(), "items": []}))
        session.commit()
        assert previous_ranking(session, date(2026, 10, 8))["date"] == "2026-09-30"
        stale = previous_ranking(session, date(2026, 10, 9))
        assert stale["stale"] is True and stale["expected_date"] == "2026-10-08"


def test_backtest_has_no_lookahead(client):
    days = _seed_many()
    with session_scope() as session:
        before = similarity_backtest(session, top=20)
        assert before["days"] and before["days"][-1]["next_date"] == days[-1].isoformat()
        assert all(row["date"] < row["next_date"] for row in before["days"])
        generate_ranking(session, days[-2], chat=_fake_chat)
        generate_ranking(session, days[-1], chat=_fake_chat)
        ai = ranking_backtest(session)
        assert [row["date"] for row in ai["days"]] == [days[-2].isoformat()]
    assert band_of(95) == "90~100" and band_of(10) == "<60"
    pool = client.get("/api/next-day-pool", params={"top": 10}).json()
    assert pool["items"] and "band_limit_up_rate" in pool["items"][0]
    for kind in ("next_day_pool", "backtest"):
        response = client.get("/api/export", params={"type": kind, "date": days[-1].isoformat()})
        assert response.status_code == 200, response.text
    assert client.get("/api/backtest", params={"kind": "ai_ranking"}).status_code == 200
