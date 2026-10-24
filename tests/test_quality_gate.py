"""数据质量闸门：十四项检查落账、脏行只删 error 级、删完只复查一遍。

负向控制在这里是必须的：一个「什么都不报」的闸门和一个「根本没跑」的闸门，
读出来的数字一模一样。所以每个「没有 error」的断言都配一条注入违规的测试，
先证明这一项判得出来，再证明干净库真的不记。
"""

from datetime import date, timedelta

from backend.app.core.config import read_settings_file
from backend.app.core.database import session_scope
from backend.app.models.tables import (
    CalcBar,
    DailyFeature,
    DailyQuote,
    DataQualityError,
    LimitUpPattern,
    PatternMatch,
    SkippedRecord,
)
from backend.app.services.purge_service import purge_condition, purge_error_rows
from backend.app.services.quality_service import (
    CHECK_NAMES,
    blocked_codes,
    counts,
    error_pairs,
    list_errors,
    run_checks,
)
from backend.app.services.sync_service import run_sync_now, set_fetcher
from tests.test_api import _save, _series, weekdays


def _seed(days: list[date], codes: tuple[str, ...] = ("600000", "000001")) -> dict[str, str]:
    names: dict[str, str] = {}
    for index, code in enumerate(codes):
        exchange = "SSE" if code.startswith("6") else "SZSE"
        name = f"测试{index}"
        names[code] = name
        _save(code, name, exchange, _series(days, limit_on={2}))
    return names


def _break(code: str, day: date, **values) -> None:
    with session_scope() as session:
        row = (
            session.query(DailyQuote)
            .filter(DailyQuote.stock_code == code, DailyQuote.trade_date == day)
            .one()
        )
        for key, value in values.items():
            setattr(row, key, value)


def _ledger() -> list[tuple[str, str, str, str]]:
    with session_scope() as session:
        return [
            (
                row.stock_code,
                row.check_name,
                row.severity,
                row.trade_date.isoformat() if row.trade_date else "",
            )
            for row in session.query(DataQualityError).order_by(
                DataQualityError.stock_code, DataQualityError.check_name, DataQualityError.trade_date
            ).all()
        ]


def _rows(code: str) -> list[date]:
    with session_scope() as session:
        return sorted(
            row.trade_date
            for row in session.query(DailyQuote).filter(DailyQuote.stock_code == code).all()
        )


# --- 检查项本身 ---------------------------------------------------------------


def test_a_clean_library_writes_nothing_but_says_it_scanned(client):
    days = weekdays(6)
    names = _seed(days)
    with session_scope() as session:
        result = run_checks(session, start=days[0], end=days[-1], names=names)
    assert result["errors"] == 0
    assert result["warnings"] == 0
    assert _ledger() == []
    #: 「没记账」有两种：真的干净，和压根没扫。回包必须能分开这两者
    assert result["scanned_days"] == len(days)
    assert result["scanned_rows"] == len(days) * 2
    assert result["skipped_checks"] == []


def test_injected_violations_land_in_the_ledger_with_their_severity(client):
    days = weekdays(6)
    _seed(days, codes=("600000",))
    _break("600000", days[-1], low=99.0)  # 高 < 低
    _break("600000", days[-2], volume=None)
    _break("600000", days[-3], amount=-1.0)
    _break("600000", days[-4], change_percent=25.0)
    _break("600000", days[-5], pre_close=13.0)
    #: 挑一个真的周六：那一天的行情是 `trade_date_check` 该抓的东西
    saturday = days[-1] + timedelta(days=5 - days[-1].weekday())
    with session_scope() as session:
        session.add(
            DailyQuote(
                stock_code="600888",
                trade_date=saturday,
                open=10,
                high=11,
                low=10,
                close=11,
                pre_close=10,
                volume=100,
                amount=1100,
                change_percent=10.0,
                source="test",
            )
        )
    with session_scope() as session:
        run_checks(session, start=days[0] - timedelta(days=10), end=saturday + timedelta(days=1))
    errors = {name for _code, name, severity, _day in _ledger() if severity == "error"}
    assert {
        "ohlc_check",
        "volume_check",
        "amount_check",
        "change_percent_check",
        "pre_close_continuity_check",
        "trade_date_check",
    } <= errors
    #: 缺行是 warning：它只记账，不挡下游，也轮不到清理那一层
    warnings = {name for _code, name, severity, _day in _ledger() if severity == "warning"}
    assert "missing_check" in warnings
    with session_scope() as session:
        assert "600000" in blocked_codes(session, days[-1])
        #: 清理只吃 error 指的「哪只哪天」；周末那条是 600888 的那一天
        assert ("600000", days[-1]) in error_pairs(session)
        assert ("600888", saturday) in error_pairs(session)


def test_the_ledger_records_each_pair_once_not_once_per_sync(client):
    days = weekdays(5)
    names = _seed(days)
    _break("600000", days[-1], low=99.0)
    with session_scope() as session:
        first = run_checks(session, start=days[0], end=days[-1], names=names)
    with session_scope() as session:
        second = run_checks(session, start=days[0], end=days[-1], names=names)
    assert first["written"] == 1
    assert second["written"] == 0
    #: 「跳过了一条已经存在的」不等于「这条没了」，回包分开说
    assert second["existing"] == 1
    assert len(_ledger()) == 1


def test_name_mismatch_is_a_warning_without_a_date(client):
    days = weekdays(5)
    _seed(days, codes=("600000",))
    with session_scope() as session:
        result = run_checks(
            session, start=days[0], end=days[-1], names={"600000": "改了名的那只"}
        )
    assert result["errors"] == 0
    assert result["warnings"] == 1
    with session_scope() as live:
        items = list_errors(live, check_name="code_name_match_check")
        assert len(items) == 1
        assert items[0]["severity"] == "warning"
        assert items[0]["trade_date"] == ""
        assert "改了名的那只" in items[0]["detail"]
        #: 名称对不上不是某一天的脏数据：它不该进按天清理的那张表
        assert error_pairs(live) == set()


def test_missing_name_list_is_reported_as_not_done(client):
    days = weekdays(5)
    _seed(days, codes=("600000",))
    with session_scope() as session:
        result = run_checks(session, start=days[0], end=days[-1])
    skipped = [item["check"] for item in result["skipped_checks"]]
    assert skipped == ["code_name_match_check"]
    assert "不是通过" in result["skipped_checks"][0]["reason"]


def test_an_empty_library_says_so_instead_of_passing(client):
    with session_scope() as session:
        result = run_checks(session, start=date(2026, 1, 5), end=date(2026, 1, 9))
    assert result["scanned_days"] == 0
    assert result["scanned_rows"] == 0
    assert result["skipped_checks"][0]["check"] == "全部十四项"


def test_summary_exposes_all_fourteen_checks(client):
    days = weekdays(4)
    names = _seed(days)
    with session_scope() as session:
        run_checks(session, start=days[0], end=days[-1], names=names)
        payload = counts(session)
    assert set(payload["by_check"]) == set(CHECK_NAMES)
    #: 那一格是给人对着表看的，必须带东八区偏移：写成 UTC，页面永远早八小时。
    assert payload["last_quality_at"].endswith("+08:00"), payload["last_quality_at"]
    assert payload["errors"] == 0


# --- 脏行清理 -----------------------------------------------------------------


def test_purge_without_confirm_deletes_nothing(client):
    days = weekdays(20)
    _seed(days, codes=("600000", "000001"))
    _break("600000", days[-1], low=99.0)
    with session_scope() as session:
        run_checks(session, start=days[0], end=days[-1])
        planned = purge_error_rows(session, start=days[0], end=days[-1])
    assert planned["dry_run"] is True
    assert planned["deleted"] == {}
    assert planned["planned"]["daily_quotes"] == 1
    assert "预演" in planned["note"]
    assert days[-1] in _rows("600000")


def test_purge_removes_the_dirty_bar_and_its_derived_rows(client):
    days = weekdays(20)
    _seed(days, codes=("600000", "000001"))
    dirty = days[-2]
    _break("600000", dirty, low=99.0)
    with session_scope() as session:
        run_checks(session, start=days[0], end=days[-1])
        session.add(CalcBar(stock_code="600000", trade_date=dirty, close=10.0))
        session.add(CalcBar(stock_code="600000", trade_date=days[-1], close=10.0))
        session.add(DailyFeature(stock_code="600000", trade_date=dirty, features={"a": 1}))
        session.add(DailyFeature(stock_code="600000", trade_date=days[-1], features={"a": 2}))
        session.add(
            SkippedRecord(trade_date=dirty, stock_code="600000", stage="scan", reason="test")
        )
        session.add(
            PatternMatch(
                target_code="600000",
                target_date=dirty,
                sample_code="000001",
                sample_limit_date=days[-3],
                similarity_score=88.0,
                score_breakdown={},
            )
        )
        session.add(
            PatternMatch(
                target_code="000001",
                target_date=days[-1],
                sample_code="600000",
                sample_limit_date=dirty,
                similarity_score=77.0,
                score_breakdown={},
            )
        )
        session.add(
            PatternMatch(
                target_code="000001",
                target_date=days[-1],
                sample_code="000001",
                sample_limit_date=days[-3],
                similarity_score=66.0,
                score_breakdown={},
            )
        )

    with session_scope() as session:
        result = purge_error_rows(session, start=days[0], end=days[-1], confirm=True)
    assert result["dry_run"] is False
    assert result["deleted"]["daily_quotes"] == 1
    assert result["deleted"]["calc_bars"] == 1
    assert result["deleted"]["daily_features"] == 1
    assert result["deleted"]["skipped_records"] == 1
    assert result["deleted"]["pattern_matches"] == 2
    assert dirty not in _rows("600000")
    assert days[-1] in _rows("600000")
    with session_scope() as session:
        assert session.query(CalcBar).filter(CalcBar.trade_date == days[-1]).count() == 1
        assert session.query(DailyFeature).filter(DailyFeature.trade_date == days[-1]).count() == 1
        #: 剩下那一条匹配两头都不脏，它不该被顺手带走
        assert session.query(PatternMatch).count() == 1
        #: 指向被删那一行的 error 账跟着走；复查新写的那条是删除动作造出来的缺口，留着
        assert (
            session.query(DataQualityError)
            .filter(DataQualityError.check_name == "ohlc_check")
            .count()
        ) == 0
        assert (
            session.query(DataQualityError)
            .filter(DataQualityError.check_name == "pre_close_continuity_check")
            .count()
        ) == 1


def test_purge_also_removes_a_sample_whose_window_sits_on_a_dirty_day(client):
    days = weekdays(20)
    _seed(days, codes=("600000", "000001"))
    dirty = days[-2]
    _break("600000", dirty, low=99.0)
    with session_scope() as session:
        run_checks(session, start=days[0], end=days[-1])
        session.add(
            LimitUpPattern(
                stock_code="600000",
                limit_up_date=days[-1],
                window_dates=[day.isoformat() for day in days[-3:]],
                feature_vector=[],
                day_details=[],
            )
        )
        session.add(
            LimitUpPattern(
                stock_code="000001",
                limit_up_date=days[-1],
                window_dates=[day.isoformat() for day in (days[-4], days[-3], days[-1])],
                feature_vector=[],
                day_details=[],
            )
        )
    with session_scope() as session:
        result = purge_error_rows(session, start=days[0], end=days[-1], confirm=True)
    #: 涨停那天自己没脏，但它那三天窗口里坐着被抽掉的一根：这一行向量再也复现不出来
    assert result["deleted"]["limit_up_patterns"] == 1
    with session_scope() as session:
        left = session.query(LimitUpPattern.stock_code).all()
    assert [row[0] for row in left] == ["000001"]


def test_purge_refuses_when_the_ratio_cap_is_hit(client):
    days = weekdays(5)
    _seed(days, codes=("600000", "000001"))
    _break("600000", days[-1], low=99.0)
    _break("600000", days[-2], volume=None)
    _break("600000", days[-3], amount=-1.0)
    _break("600000", days[-4], change_percent=25.0)
    with session_scope() as session:
        run_checks(session, start=days[0], end=days[-1])
        result = purge_error_rows(session, start=days[0], end=days[-1], confirm=True)
    assert result["ok"] is False
    assert "上限" in result["refused"]
    assert result["deleted"] == {}
    assert len(_rows("600000")) == len(days)
    with session_scope() as session:
        assert (session.query(DataQualityError).filter(DataQualityError.severity == "error").count()) == 4


def test_the_recheck_reports_new_errors_but_never_deletes_them(client):
    days = weekdays(20)
    _seed(days, codes=("600000", "000001"))
    middle = days[-2]
    _break("600000", middle, low=99.0)
    with session_scope() as session:
        run_checks(session, start=days[0], end=days[-1])
        result = purge_error_rows(session, start=days[0], end=days[-1], confirm=True)
    #: 中间那根被抽掉之后，后面那根的 pre_close 就接不上了。这是删除动作造出来的缺口，
    #: 不是原因——照它再删一轮会顺着时间轴把历史啃穿，所以只报不删。
    exposed = [item["check"] for item in result["newly_exposed"]]
    assert "pre_close_continuity_check" in exposed
    assert days[-1] in _rows("600000")
    assert result["recheck"]["errors"] >= 1
    with session_scope() as session:
        again = purge_condition(session)
    assert again["error_pairs"] >= 1


# --- 同步里真的跑这一趟 -------------------------------------------------------


def test_sync_records_the_source_ledger_and_runs_the_checks(client):
    days = weekdays(5)

    def kline(code, start, end):
        rows = _series(days, limit_on={3}, start=10.0)
        rows[-1]["low"] = 99.0
        return rows

    set_fetcher(lambda: [("600000", "浦发银行")], kline)
    result = run_sync_now()
    assert result["progress"] == 100
    assert "质量检查" in result["message"]
    assert "1 条 error" in result["message"]
    sources = read_settings_file()["last_sync_sources"]
    assert sources["sources"] == {"eastmoney": 1}
    assert sources["saved"] == 1
    assert sources["quality_errors"] == 1
    assert sources["quality_scanned_days"] == len(days)
    status = client.get("/api/sync/status").json()
    assert status["last_sync_sources"]["sources"] == {"eastmoney": 1}


def test_sync_says_out_loud_when_the_checks_cannot_run(client, monkeypatch):
    days = weekdays(4)
    set_fetcher(lambda: [("600000", "浦发银行")], lambda code, start, end: _series(days, limit_on={3}))

    def boom(*args, **kwargs):
        raise RuntimeError("账表写不进去")

    monkeypatch.setattr("backend.app.services.quality_service.run_checks", boom)
    result = run_sync_now()
    assert "质量检查没有跑成" in result["message"]
    assert "账表写不进去" in result["message"]


# --- 接口 --------------------------------------------------------------------


def test_quality_routes_report_read_and_previews(client):
    days = weekdays(20)
    _seed(days, codes=("600000", "000001"))
    _break("600000", days[-1], low=99.0)

    checked = client.post(
        "/api/quality/check",
        params={"start": days[0].isoformat(), "end": days[-1].isoformat()},
    ).json()
    assert checked["errors"] == 1
    assert checked["skipped_checks"][0]["check"] == "code_name_match_check"

    listed = client.get("/api/quality/errors", params={"severity": "error"}).json()
    assert listed["count"] == 1
    assert listed["items"][0]["check"] == "ohlc_check"
    assert listed["items"][0]["check_label"]

    summary = client.get("/api/quality/summary").json()
    assert len(summary["checks"]) == 14
    assert summary["errors"] == 1
    assert summary["purge"]["error_pairs"] == 1
    assert summary["purge"]["checks"] == ["开高低收缺项、非正数或高低关系不成立"]

    preview = client.post(
        "/api/quality/purge", params={"start": days[0].isoformat(), "end": days[-1].isoformat()}
    ).json()
    assert preview["dry_run"] is True
    assert preview["planned"]["daily_quotes"] == 1

    done = client.post(
        "/api/quality/purge",
        params={"start": days[0].isoformat(), "end": days[-1].isoformat(), "confirm": "true"},
    ).json()
    assert done["deleted"]["daily_quotes"] == 1
    assert client.get("/api/quality/summary").json()["purge"]["error_pairs"] == 0


def test_quality_routes_reject_a_stray_check_name(client):
    assert client.get("/api/quality/errors", params={"severity": "bad"}).status_code == 422
    assert client.get("/api/quality/errors", params={"check": "nope"}).status_code == 422
