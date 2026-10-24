"""档3（后半）：涨停样本库、匹配明细落库与数据管理页那四个任务。

这一份文件盯的是**一把尺子**：

* 样本向量落库前后必须逐维相等（`build_three_day_window` 只有一个口径）；
* 落库的匹配分必须等于现算的匹配分（`score_day` 是唯一写入路径，没有第二套打分）；
* 页面读数吃库里那一版，不是现算那一版（GET 只读、POST 才重算）。

负向控制照常配齐：闸门挡人时同一天必须有干净样本活着；没有明细时接口给的是
「为什么没有」，不是一张空表，也不是 0%。
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime
from urllib.parse import unquote

import pytest
from sqlalchemy import select
from seeds import save_stock, seed, series, weekdays

from backend.app.algorithms.features import (
    AGGREGATE_NAMES,
    DAILY_SPEC,
    VECTOR_DIM,
    build_three_day_window,
    feature_names,
)
from backend.app.core.clock import now_naive
from backend.app.core.database import session_scope
from backend.app.models.tables import (
    DailyQuote,
    DataQualityError,
    LimitUpPattern,
    MatchSummary,
    PatternMatch,
    SkippedRecord,
)
from backend.app.services import (
    ai_evidence,
    data_job,
    feature_service,
    match_service,
    pattern_service,
    retention,
)
from backend.app.services.backtest_service import score_day
from backend.app.services.history_backfill import load_bars
from backend.app.services.quality_service import ERROR
from backend.app.services.retention import quote_dates
from backend.app.services.similarity_service import similar_to

#: 默认夹具里 12 个交易日落进样本库的那六条 (代码, 涨停日)。
EXPECTED_SAMPLES = {
    ("600000", 4),
    ("600001", 5),
    ("000002", 6),
    ("600000", 7),
    ("600001", 8),
    ("600000", 9),
}


# --- 读库的小工具：一律在会话里折成普通 dict，提交后对象就过期了 --------------


def _stored_pattern(code: str, day: date) -> dict | None:
    with session_scope() as session:
        row = pattern_service.pattern_row(session, code, day)
        if row is None:
            return None
        return {
            "window": list(row.window_dates or []),
            "vector": [float(value) for value in (row.feature_vector or [])],
            "details": [dict(item) for item in (row.day_details or [])],
            "aggregates": pattern_service.aggregates_of(row),
        }


def _pattern_index(days: list[date]) -> set[tuple[str, date]]:
    with session_scope() as session:
        return {
            (row.stock_code, row.limit_up_date)
            for row in session.query(LimitUpPattern).filter(
                LimitUpPattern.limit_up_date.in_(days)
            )
        }


def _live_window(code: str, day: date, *, include_as_of_date: bool):
    with session_scope() as session:
        return build_three_day_window(
            load_bars(session, code, day), day, include_as_of_date=include_as_of_date
        )


def _pairs(day: date, code: str | None = None) -> dict[tuple[str, str], float]:
    with session_scope() as session:
        stmt = select(PatternMatch).where(PatternMatch.target_date == day)
        if code is not None:
            stmt = stmt.where(PatternMatch.target_code == code)
        rows = list(session.scalars(stmt).all())
    return {
        (row.target_code, row.sample_limit_date.isoformat()): round(float(row.similarity_score), 2)
        for row in rows
    }


def _build_all_patterns(days: list[date]) -> dict:
    """整段重建（跳过账按天累加，返回这一轮的总账）。"""
    with session_scope() as session:
        stats = feature_service.new_stats("build_patterns", days[0], days[-1])
        for day in days:
            pattern_service.build_patterns(session, day, stats=stats)
        return stats


def _build_one_pattern(day: date) -> dict:
    with session_scope() as session:
        stats = feature_service.new_stats("build_patterns", day, day)
        pattern_service.build_patterns(session, day, stats=stats)
        return stats


def _scan(day: date) -> dict:
    with session_scope() as session:
        stats = feature_service.new_stats("scan_market", day, day)
        return match_service.scan_and_store(session, day, stats=stats)


def _wait_job(client, *, timeout: float = 60.0) -> dict:
    """轮询 `/api/data/job` 直到后台线程收工。页面读进度就靠这一拍，不靠重进页面。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/data/job").json()
        if not state["running"]:
            assert state["error"] == "", f"后台任务失败：{state['error']}"
            return state
        time.sleep(0.05)
    raise AssertionError("后台重建任务没有在规定时间内跑完")


def _inject_error(code: str, day: date) -> None:
    with session_scope() as session:
        session.add(
            DataQualityError(
                stock_code=code,
                trade_date=day,
                check_name="volume_check",
                severity=ERROR,
                detail="测试注入",
            )
        )


# --- 样本库：落库的向量与现算的向量是同一把尺子 --------------------------------


def test_stored_vector_equals_a_freshly_computed_window(client):
    """逐维比较库里那一条与现算那一条。谁改了口径当场炸。"""
    days = seed()
    stats = _build_all_patterns(days)
    assert stats["pattern_count"] == 6
    assert {(code, days.index(day)) for code, day in _pattern_index(days)} == EXPECTED_SAMPLES

    for code, limit_day in sorted(_pattern_index(days)):
        stored = _stored_pattern(code, limit_day)
        live = _live_window(code, limit_day, include_as_of_date=False)
        assert live.ok, (code, limit_day, live.reason)
        assert len(stored["vector"]) == VECTOR_DIM == len(live.vector) == 67
        assert stored["vector"] == pytest.approx([float(value) for value in live.vector])
        assert stored["window"] == [bar.trade_date.isoformat() for bar in live.days]
        # 涨停当天不进向量：含进去就等于拿结果当特征。
        assert all(item < limit_day.isoformat() for item in stored["window"])
        assert [item["trade_date"] for item in stored["details"]] == stored["window"]
        for stored_day, live_day in zip(stored["details"], live.day_features):
            for name, _group in DAILY_SPEC:
                assert stored_day["features"][name] == pytest.approx(live_day[name])
        assert list(stored["aggregates"]) == AGGREGATE_NAMES
        tail = [float(value) for value in live.vector[len(DAILY_SPEC) * 3 :]]
        assert list(stored["aggregates"].values()) == pytest.approx(tail)


def test_an_stale_length_vector_yields_no_aggregates(client):
    """旧口径那行（长度不对）不凑聚合值：截断补零会造出一批没人算过的数。"""
    days = seed()
    _build_all_patterns(days)
    with session_scope() as session:
        row = session.scalars(
            select(LimitUpPattern).where(
                LimitUpPattern.stock_code == "600000", LimitUpPattern.limit_up_date == days[9]
            )
        ).one()
        row.feature_vector = [0.0] * (VECTOR_DIM - 1)
    stored = _stored_pattern("600000", days[9])
    assert len(stored["vector"]) == VECTOR_DIM - 1
    assert stored["aggregates"] is None


def test_the_quality_gate_blocks_one_sample_and_keeps_its_siblings(client):
    """error 级异常只挡够得着的那一条样本，同一天干净的兄弟必须活着。

    注入点取形态三天的**最后一天**（`window[-1]`）。取中间那天会变成挡两条——
    那天恰好也是 600000 自己的一次涨停日，「涨停当天脏」会把它前后两条样本一起拦下，
    那是另一条判据（见 `test_a_day_scoped_error_does_not_blanket_the_whole_day`）。
    """
    days = seed()
    limit_day = days[9]
    assert _build_all_patterns(days)["skipped"] == {}
    window = _stored_pattern("600000", limit_day)["window"]
    dirty_day = date.fromisoformat(window[-1])
    assert dirty_day == days[8] and window[-1] < limit_day.isoformat()

    _inject_error("600000", dirty_day)
    stats = _build_all_patterns(days)
    assert stats["skipped"][pattern_service.SKIP_QUALITY_ERROR] == 1
    assert _stored_pattern("600000", limit_day) is None, "形态三天里有一天是脏的，这条样本却还在库里"
    assert {(code, days.index(day)) for code, day in _pattern_index(days)} == {
        key for key in EXPECTED_SAMPLES if key != ("600000", 9)
    }, "干净的兄弟被一起挡掉了，或者脏的那条还留在库里"

    with session_scope() as session:
        page = feature_service.skip_page(
            session, stage="pattern", reason=pattern_service.SKIP_QUALITY_ERROR
        )
    assert page["total"] == 1
    assert page["items"][0]["stock_code"] == "600000"
    assert date.fromisoformat(page["items"][0]["trade_date"]) == limit_day
    assert dirty_day.isoformat() in page["items"][0]["detail"]


def test_a_day_scoped_error_does_not_blanket_the_whole_day(client):
    """反向对照：脏行只属于 600000，同一天另一只干净票照样出样本。

    没有这一条，上一条判据可以由「一有 error 就把整天封掉」这种坏实现满足。
    """
    days = seed(limit_indices={4, 7})
    limit_day = days[7]
    _inject_error("600000", limit_day)
    _build_one_pattern(limit_day)
    codes = {code for code, _day in _pattern_index([limit_day])}
    assert "600000" not in codes, "涨停当天有 error 的那只票进了样本库"
    assert codes == {"000002"}, f"同一天干净的样本被一起挡掉了：{codes}"


def test_a_sample_without_three_prior_bars_is_a_skip_not_a_padded_row(client):
    """凑不齐涨停前三根线：记 `insufficient_window`，不拿零补出一个假样本。"""
    days = seed(limit_indices={1, 3})
    stats = _build_one_pattern(days[1])
    assert stats["pattern_count"] == 0
    assert stats["skipped"][pattern_service.SKIP_INSUFFICIENT_WINDOW] == 1
    with session_scope() as session:
        page = feature_service.skip_page(
            session,
            stage="pattern",
            reason=pattern_service.SKIP_INSUFFICIENT_WINDOW,
            day=days[1],
        )
    assert page["total"] == 1 and page["items"][0]["stock_code"] == "600000"

    # 正向对照：再往后两天，前置线够了，同一批票就出得了样本。
    later = _build_one_pattern(days[3])
    assert later["pattern_count"] == 2 and later["skipped"] == {}


def test_build_patterns_is_idempotent_and_only_rewrites_that_day(client):
    """重跑不产生第二行，也不动别的天：样本按涨停日整天替换。"""
    days = seed()
    _build_all_patterns(days)
    once = {(code, day): _stored_pattern(code, day) for code, day in _pattern_index(days)}
    _build_all_patterns([days[9]])
    twice = {(code, day): _stored_pattern(code, day) for code, day in _pattern_index(days)}
    assert once == twice, "重建一天把别的天也改了，或者留下第二行"

    with session_scope() as session:
        row = session.scalars(
            select(DailyQuote).where(
                DailyQuote.stock_code == "600000", DailyQuote.trade_date == days[7]
            )
        ).one()
        row.close = float(row.close) * 1.1
        row.volume = 9_999_999
    _build_all_patterns([days[9]])
    moved = _stored_pattern("600000", days[9])
    assert len(moved["vector"]) == VECTOR_DIM
    assert moved["vector"] != once[("600000", days[9])]["vector"], "改了形态窗口的行情，向量却纹丝不动"
    assert {code for code, day in _pattern_index(days) if day == days[9]} == {"600000"}


# --- 匹配落库：库里那一行就是现算那一分 ---------------------------------------


def test_a_dimension_without_any_amount_data_is_not_a_perfect_score(client):
    """整列没有成交额时，「成交额分」是 None，不是零向量对零向量的满分 100。

    新浪那一路 `amount` 实测整列为空，三个成交额特征只能补 0；两边都补 0 时
    `score_matrix` 的余弦初值是 1、欧氏距离是 0，那一格会报成 100——
    页面上写成「成交额高度相似」就成了凭空造出来的证据。
    正向对照在前：夹具的成交额有数，那一格就该是个数。
    """
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    with session_scope() as session:
        scored = score_day(session, day, top_matches=3)
    pairs = [match for item in scored["items"] for match in item["matches"]]
    assert pairs and all(match["amount_score"] is not None for match in pairs)
    assert "NaN" not in client.get(
        f"/api/similarity?code={scored['items'][0]['code']}&date={day.isoformat()}"
    ).text

    with session_scope() as session:
        session.query(DailyQuote).update({DailyQuote.amount: None}, synchronize_session=False)
    _build_all_patterns(days)
    _scan(day)
    code = scored["items"][0]["code"]
    with session_scope() as session:
        blank = score_day(session, day, top_matches=3)
        stored = match_service.stored_matches(session, code, day)
    assert all(
        match["amount_score"] is None for item in blank["items"] for match in item["matches"]
    ), "没有成交额数据的那一格还在报满分"
    assert all(item["amount_score"] is None for item in stored["items"]), "落库把 NaN 抄成了一个数"
    #: NaN 一旦出到 JSON，浏览器那句 `response.json()` 是整页解析失败，不是一格空值。
    body = client.get(f"/api/similarity?code={code}&date={day.isoformat()}").text
    assert "NaN" not in body, "NaN 出到了接口层"
    file = client.get(f"/api/matches/export?date={day.isoformat()}&code={code}")
    assert b"nan" not in file.content.lower(), "CSV 里也不许留下 nan 那种字样"


def test_stored_match_scores_equal_the_live_scan(client):
    """接口现算出来的分与库里那一行的分逐条相等，分项、档位、窗口也一样。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    written = _scan(day)
    with session_scope() as session:
        scored = score_day(session, day, top_matches=20)
    live = {
        (item["code"], match["code"], match["limit_date"]): match
        for item in scored["items"]
        for match in item["matches"]
    }
    assert scored["pool_size"] == 2
    assert written["match_count"] == len(live) == 6
    assert written["summary_count"] == written["target_count"] == len(scored["items"]) == 4

    with session_scope() as session:
        rows = list(
            session.scalars(select(PatternMatch).where(PatternMatch.target_date == day)).all()
        )
    assert len(rows) == len(live)
    #: 现算那份的排名就是 `matches` 里的顺序（分数从高到低），落库的 `rank` 必须跟着它。
    live_rank = {
        (item["code"], match["code"], match["limit_date"]): index + 1
        for item in scored["items"]
        for index, match in enumerate(item["matches"])
    }
    for row in rows:
        key = (row.target_code, row.sample_code, row.sample_limit_date.isoformat())
        match = live[key]
        assert round(float(row.similarity_score), 2) == match["score"], key
        breakdown = dict(row.score_breakdown or {})
        assert breakdown["band"] == match["band"], key
        assert breakdown["shape"] == match["shape_score"], key
        assert breakdown["volume"] == match["volume_score"], key
        assert breakdown["volume_pct"] == match["volume_pct_score"], key
        assert breakdown["amount"] == match["amount_score"], key
        assert breakdown["window"] == match["window"], key
        assert breakdown["name"] == match["name"], key
        assert int(breakdown["rank"]) == live_rank[key], key


def test_a_code_that_limited_up_today_never_becomes_a_match_target(client):
    """当天已涨停的那只票不进候选：它的形态里含着涨停那一天。"""
    days = seed()
    _build_all_patterns(days)
    day = days[9]
    _scan(day)
    with session_scope() as session:
        scored = score_day(session, day, top_matches=20)
    assert scored["limit_up_count"] == 1
    assert {item["code"] for item in scored["items"]} == {"600001", "000001", "000002"}
    assert all(target != "600000" for target, _sample in _pairs(day))


def test_the_stored_detail_reads_the_database_and_matches_the_home_page(client):
    """GET 读库不现算；库里的最高分与首页那条路（`similar_to`）给的是同一个数。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    _scan(day)
    before = len(_pairs(day))
    with session_scope() as session:
        stored = match_service.stored_matches(session, "000001", day)
        live = similar_to(session, "000001", day, 5)
    assert stored["ok"] is True and stored["reason"] == match_service.STORED
    assert [item["rank"] for item in stored["items"]] == list(range(1, len(stored["items"]) + 1))
    assert [item["score"] for item in stored["items"]] == sorted(
        (item["score"] for item in stored["items"]), reverse=True
    )
    assert stored["summary"]["max_similarity"] == max(match["score"] for match in live["matches"])
    assert stored["pool_size"] == live["pool_size"] == 2
    assert stored["pool_dates"] == [item.isoformat() for item in days[8:11]]
    assert stored["stock_name"] == "平安银行"
    #: `created_at` 那列不带时区，存的必须是东八区挂钟：写 UTC 的话页面上「落库时间」永远早八小时。
    stamped = datetime.fromisoformat(stored["summary"]["created_at"])
    assert abs((now_naive() - stamped).total_seconds()) < 600, stored["summary"]["created_at"]
    assert len(_pairs(day)) == before, "GET 读了库却顺手写了一行"


def test_a_single_recompute_writes_the_same_rows_as_the_market_scan(client):
    """`match_target` 与全市场扫描共用 `score_day`：同一只票同一天不可能有两个分。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    _scan(day)
    market = _pairs(day, "000002")
    with session_scope() as session:
        payload = match_service.match_target(session, "000002", day)
    assert payload["ok"] is True
    assert payload["written"] == {"match_count": len(market), "summary_count": 1, "target_count": 1}
    assert _pairs(day, "000002") == market, "重算这一只把它的分改成了另一个数"
    assert {item["score"] for item in payload["items"]} == set(market.values())


def test_missing_detail_names_the_reason_instead_of_returning_an_empty_table(client):
    """三种「没有明细」分开说：没行情、当天涨停、还没扫过。混成一句「查不到」用户只能瞎试。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    with session_scope() as session:
        no_quote = match_service.stored_matches(session, "600999", day)
        not_scanned = match_service.stored_matches(session, "000001", day)
        limit_up = match_service.stored_matches(session, "600000", days[9])
        target = match_service.match_target(session, "600000", days[9])
    for payload in (no_quote, not_scanned, limit_up, target):
        assert payload["ok"] is False
        assert payload["items"] == [] and payload["summary"] is None
        assert payload["detail"] and payload["disclaimer"]
    assert no_quote["reason"] == match_service.NO_QUOTE
    assert not_scanned["reason"] == "not_scanned"
    assert limit_up["reason"] == target["reason"] == match_service.LIMIT_UP_TODAY


def test_an_empty_pool_is_said_out_loud(client):
    """池子空的这一天：`ok:false` + `empty_pool`，不是「0 条相似」那张空表。"""
    days = seed()
    day = days[0]
    with session_scope() as session:
        stats = feature_service.new_stats("scan_market", day, day)
        match_service.scan_and_store(session, day, stats=stats)
        payload = match_service.match_target(session, "000001", day)
    assert payload["ok"] is False and payload["reason"] == match_service.EMPTY_POOL
    assert stats["skipped"][match_service.EMPTY_POOL] == 1
    assert stats["match_count"] == stats["summary_count"] == 0
    assert _pairs(day) == {}


def test_re_scan_replaces_the_day_rather_than_appending_to_it(client):
    """先删这批 (代码, 日) 的旧行再写：重跑一次只留一份答案，不留旧口径的残行。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    _scan(day)
    first = _pairs(day)
    with session_scope() as session:
        session.add(
            PatternMatch(
                target_code="000001",
                target_date=day,
                sample_code="000002",
                sample_limit_date=days[3],
                similarity_score=99.99,
                score_breakdown={"rank": 99, "name": "旧口径的残行"},
            )
        )
    assert ("000001", days[3].isoformat()) in _pairs(day)
    _scan(day)
    assert _pairs(day) == first, "重扫一天之后还留着上一轮的残行"
    with session_scope() as session:
        assert int(session.query(MatchSummary).filter(MatchSummary.target_date == day).count()) == 4


# --- 逐日对比 ------------------------------------------------------------------


def test_compare_pairs_every_daily_feature_of_both_windows(client):
    """左右对比：3 天 × 20 项 = 60 行差值，聚合那 7 项两边都在，分数取自库里那一行。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    _scan(day)
    with session_scope() as session:
        stored = match_service.stored_matches(session, "000001", day)
        top = stored["items"][0]
        payload = match_service.compare_with_sample(
            session, "000001", day, top["code"], date.fromisoformat(top["limit_date"])
        )
    assert [item["field"] for item in payload["differences"]] == [
        name for _ in range(3) for name, _group in DAILY_SPEC
    ]
    assert [item["day"] for item in payload["differences"]] == (
        ["T-3"] * 20 + ["T-2"] * 20 + ["T-1"] * 20
    )
    assert {item["group"] for item in payload["differences"]} == {
        group for _name, group in DAILY_SPEC
    }
    for row in payload["differences"]:
        if isinstance(row["target"], (int, float)) and isinstance(row["sample"], (int, float)):
            assert row["difference"] == pytest.approx(row["target"] - row["sample"], abs=1e-6)
        else:
            assert row["difference"] is None, "任何一边缺值时差值是 None，不是 0"
    assert list(payload["target"]["aggregates"]) == AGGREGATE_NAMES
    assert list(payload["sample"]["aggregates"]) == AGGREGATE_NAMES
    assert payload["score"] == top["score"]
    assert len(payload["target"]["days"]) == len(payload["sample"]["days"]) == 3
    assert payload["target"]["as_of_date"] == day.isoformat()
    assert payload["sample"]["as_of_date"] == top["limit_date"]
    assert payload["target"]["name"] == "平安银行"
    assert payload["disclaimer"]


def test_compare_refuses_a_missing_sample_and_an_impossible_target(client):
    """样本没落库 → LookupError；目标凑不齐三天 → ValueError。两种错不是一种。"""
    days = seed()
    _build_all_patterns(days)
    save_stock("600060", "只有两天线", "SSE", series(days, limit_on=set(), start=9.0)[-2:])
    with session_scope() as session:
        with pytest.raises(LookupError) as missing:
            match_service.compare_with_sample(session, "000001", days[10], "600000", days[0])
        with pytest.raises(ValueError) as thin:
            match_service.compare_with_sample(session, "600060", days[-1], "600000", days[9])
    assert days[0].isoformat() in str(missing.value)
    assert "凑不齐完整三日形态" in str(thin.value)

    # 没落库的那一对：分数是 None，不现算一个数冒充同一个口径。
    with session_scope() as session:
        payload = match_service.compare_with_sample(session, "000001", days[10], "600000", days[7])
    assert payload["score"] is None


# --- CSV 与那份「高档占比」 ----------------------------------------------------


def test_export_csv_carries_the_stored_pairs_and_says_when_it_trims(client):
    """导出吃落库那一份：表头、行数、截断标志都对得上；截断按「前几只目标」算。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    _scan(day)
    pairs = _pairs(day)
    with session_scope() as session:
        text, rows, truncated = match_service.export_csv(session, day)
        one_code, code_rows, code_flag = match_service.export_csv(session, day, code="000001")
        trimmed, trim_rows, trim_flag = match_service.export_csv(session, day, top_targets=1)
    assert "\r\n" in text and text.endswith("\r\n")
    lines = text.splitlines()
    assert lines[0] == ",".join(match_service.CSV_COLUMNS)
    assert rows == len(pairs) == 6 and len(lines) == rows + 1
    assert truncated is False
    for line in lines[1:]:
        assert line.split(",")[2] == day.isoformat()

    assert code_rows == len(_pairs(day, "000001")) == 2
    assert code_flag is False
    assert len(one_code.splitlines()) == code_rows + 1

    assert trim_flag is True, "只给了前 1 只目标却不声明截断，等于默默少给数据"
    assert 0 < trim_rows < rows
    assert len(trimmed.splitlines()) == trim_rows + 1


def test_high_similarity_share_stays_null_when_nothing_was_scanned(client):
    """没跑过扫描时 `high_rate_percent` 是 None：把「没有明细」当成 0% 会压低风险分。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    with session_scope() as session:
        before = match_service.high_similarity_share(session, day)
    assert before == {"targets": 0, "high_rate_percent": None, "source": "no_scan"}

    _scan(day)
    with session_scope() as session:
        after = match_service.high_similarity_share(session, day)
        summaries = session.query(MatchSummary).filter(MatchSummary.target_date == day).all()
    highs = sum(int(row.high_similarity_count or 0) for row in summaries)
    totals = sum(int(row.sample_count or 0) for row in summaries)
    assert after["source"] == "match_summaries" and after["targets"] == len(summaries) == 4
    assert after["high_threshold"] == 90.0
    assert after["high_rate_percent"] == (None if totals <= 0 else round(highs / totals * 100.0, 2))


def test_carry_evidence_reads_stored_matches_and_names_its_base_day(client):
    """连板率那份证据的原料是 `pattern_matches`：没落库时说「没落库」，不编 0%。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    codes = ["000001", "600001"]
    with session_scope() as session:
        empty, empty_meta = ai_evidence.carry_overview(session, codes, day)
        no_base, no_base_meta = ai_evidence.carry_overview(session, codes, None)
    assert empty == {} and empty_meta == {"source": "no_stored_matches", "targets": 0, "rows": 0}
    assert no_base == {} and no_base_meta["source"] == "no_base_date"

    _scan(day)
    with session_scope() as session:
        rows, meta = ai_evidence.carry_overview(session, codes, day)
        grouped = match_service.matches_by_code(session, day, codes)
    assert meta["source"] == "pattern_matches" and meta["base_date"] == day.isoformat()
    assert meta["targets"] == len(rows) > 0
    assert meta["rows"] == sum(len(value) for value in grouped.values())
    for code, item in rows.items():
        assert item["carry_match_count"] == len(grouped[code])
        assert set(item) == {
            "carry_match_count",
            "carry_next_day_rate_percent",
            "carry_known",
            "carry_missing",
        }
    with session_scope() as session:
        top = match_service.matches_by_code(session, day, ["000001"])["000001"]
    assert len(top) <= 20 and [item["score"] for item in top] == sorted(
        (item["score"] for item in top), reverse=True
    )


# --- 接口层：概况、任务、读数与导出 -------------------------------------------


def test_data_status_counts_only_what_is_in_the_database(client):
    """概况报的是库里真有的行：重建之前那四张派生表都是 0。"""
    days = seed()
    blank = client.get("/api/data/status").json()
    assert blank["counts"]["daily_quotes"] == 4 * len(days)
    for key in (
        "daily_features",
        "limit_up_patterns",
        "pattern_matches",
        "match_summaries",
        "stock_training_rows",
    ):
        assert blank["counts"][key] == 0, key
    assert (
        blank["patterns_by_date"]
        == blank["features_by_date"]
        == blank["matches_by_date"]
        == blank["training_by_date"]
        == {}
    )
    assert blank["earliest_trade_date"] == days[0].isoformat()
    assert blank["latest_trade_date"] == days[-1].isoformat()
    assert blank["trade_day_count"] == len(days)
    assert blank["pool_dates"] == [item.isoformat() for item in days[-3:]]
    assert blank["compare_window_days"] == 3 and blank["running_job"] == ""
    assert blank["training"]["rows"] == 0 and blank["training"]["blocked_rows"] == 0
    assert blank["retention_trading_days"] > 0

    for path, job in (
        ("/api/data/rebuild-features", "rebuild_features"),
        ("/api/data/rebuild-patterns", "build_patterns"),
        ("/api/data/scan-market", "scan_market"),
        ("/api/data/build-training", "build_training"),
    ):
        assert client.post(path).status_code == 200, path
        state = _wait_job(client)
        assert state["result"] is not None and state["result"]["job"] == job, path

    after = client.get("/api/data/status").json()
    assert after["counts"]["daily_features"] == 4 * len(days)
    # 样本与扫描留空只覆盖样本池那三天：600000 在 days[9] 那一条。
    assert after["counts"]["limit_up_patterns"] == 1
    assert after["counts"]["pattern_matches"] == 3 and after["counts"]["match_summaries"] == 3
    assert after["counts"]["stock_training_rows"] == 28
    assert sorted(after["features_by_date"]) == [item.isoformat() for item in days]
    assert after["features_by_date"][days[-1].isoformat()] == 4
    assert sum(after["patterns_by_date"].values()) == 1
    assert sum(after["matches_by_date"].values()) == after["counts"]["match_summaries"]
    assert sum(after["training_by_date"].values()) == after["counts"]["stock_training_rows"]
    assert after["skips"]["total"] == sum(after["skips"]["by_stage"].values())
    assert after["training"]["rows"] == 28 and after["training"]["blocked_rows"] == 0
    #: 这一轮没注入过 error，质量账必须还是空的——概况里那一格数的是账表，不是「跑过任务」。
    assert after["quality"]["errors"] == after["quality"]["total"] == 0
    assert after["quality"]["latest_date"] == ""


def test_a_second_job_is_answered_with_409_naming_the_occupant(client):
    """槽位只有一个：第二个请求直接 409，并说清是谁占着。"""
    days = seed()
    released = threading.Event()
    started = threading.Event()

    def blocking(session, day, acc):
        started.set()
        assert released.wait(20.0), "测试没把占位线程放出来"

    data_job.reset_state()
    data_job.start("features", dates=[days[-1]], work=blocking)
    assert started.wait(5.0), "占位任务没起来，409 这条判据就没意义了"
    try:
        answer = client.post("/api/data/rebuild-patterns")
        assert answer.status_code == 409
        assert "重新计算特征" in answer.json()["detail"]
        assert client.get("/api/data/status").json()["running_job"] == "重新计算特征"
        assert client.get("/api/data/job").json()["running"] is True
    finally:
        released.set()
        data_job.reset_state()

    assert client.post("/api/data/rebuild-patterns").status_code == 200
    _wait_job(client)
    assert client.get("/api/data/status").json()["running_job"] == ""
    assert client.get("/api/data/job").json()["result"]["job"] == "build_patterns"


def test_an_empty_database_refuses_the_job_instead_of_starting_a_thread(client):
    """库里没行情：当场说清楚，不起一个跑完才发现无事可做的线程。"""
    assert client.get("/api/data/status").json()["trade_day_count"] == 0
    for path in (
        "/api/data/rebuild-features",
        "/api/data/rebuild-patterns",
        "/api/data/scan-market",
        "/api/data/build-training",
    ):
        answer = client.post(path)
        assert answer.status_code == 404, path
        assert "先执行同一步同步" in answer.json()["detail"]
    assert client.get("/api/data/job").json()["running"] is False


def test_an_illegal_range_is_answered_before_the_thread_starts(client):
    """起始日晚于结束日：422，并且不占槽位。"""
    days = seed()
    answer = client.post(
        f"/api/data/rebuild-features?start={days[-1].isoformat()}&end={days[0].isoformat()}"
    )
    assert answer.status_code == 422
    assert "晚于" in answer.json()["detail"]
    assert client.get("/api/data/job").json()["running"] is False


def test_pattern_pages_read_the_stored_samples(client):
    """涨停形态页与单票明细读的都是库里那一版。"""
    days = seed()
    _build_all_patterns(days)
    day = days[-1]
    stored = _stored_pattern("600000", days[9])

    overview = client.get(f"/api/patterns/overview?date={day.isoformat()}").json()
    assert overview["trade_date"] == day.isoformat()
    assert overview["pool_dates"] == [item.isoformat() for item in days[-3:]]
    assert overview["dimension"] == VECTOR_DIM
    assert overview["feature_names"] == [name for name, _group in DAILY_SPEC]
    assert overview["aggregate_names"] == AGGREGATE_NAMES
    assert overview["sample_total"] == 1
    assert [item["limit_date"] for item in overview["days"]] == [
        item.isoformat() for item in sorted(days[-3:], reverse=True)
    ]
    row = next(item for item in overview["days"] if item["limit_date"] == days[9].isoformat())
    assert row["count"] == 1 and row["skipped"] == {}
    assert row["items"][0]["code"] == "600000"
    assert row["items"][0]["name"] == "浦发银行"
    assert row["items"][0]["window"] == stored["window"]
    assert row["items"][0]["dimension"] == VECTOR_DIM
    assert row["items"][0]["limit_up_percent"] is not None
    assert row["items"][0]["change_percent"] is not None
    assert "不是涨停概率" in overview["disclaimer"]
    assert overview["reason_labels"] == pattern_service.REASON_LABELS

    detail = client.get("/api/patterns/600000").json()
    assert detail["name"] == "浦发银行"
    assert [item["limit_date"] for item in detail["samples"]] == [
        item.isoformat() for item in sorted([days[4], days[7], days[9]], reverse=True)
    ]
    assert detail["samples"][0]["vector"] == pytest.approx(stored["vector"])
    assert detail["samples"][0]["aggregates"] == stored["aggregates"]
    assert len(detail["samples"][0]["days"]) == 3
    assert set(detail["samples"][0]["days"][0]["features"]) == set(overview["feature_names"])
    assert detail["samples"][0]["days"][0]["quote"]["close"] is not None
    assert all(len(item["days"]) == 3 for item in detail["samples"])

    top = client.get(f"/api/patterns/top?date={day.isoformat()}").json()
    assert top["sample_total"] == overview["sample_total"] == 1
    assert "days" not in top["items"][0] and "vector" not in top["items"][0]

    assert client.get("/api/patterns/overview?date=2020-01-01").status_code == 404
    missing = client.get("/api/patterns/000001")
    assert missing.status_code == 404 and "重建涨停样本" in missing.json()["detail"]


def test_match_routes_read_then_recompute_and_export_a_bom(client):
    """`/matches` 那一组：GET 读库、POST 重算、导出带 BOM 与行数头。"""
    days = seed()
    _build_all_patterns(days)
    day = days[10]
    code = "000001"
    empty = client.get(f"/api/matches/{code}?date={day.isoformat()}").json()
    assert empty["ok"] is False and empty["reason"] == "not_scanned"

    scanned = client.post(f"/api/matches/{code}?date={day.isoformat()}").json()
    assert scanned["ok"] is True and scanned["written"]["target_count"] == 1
    again = client.get(f"/api/matches/{code}?date={day.isoformat()}").json()
    assert again["items"] == scanned["items"] and again["summary"] == scanned["summary"]

    # 再补一只，`top=1` 才有东西可截。
    assert client.post(f"/api/matches/000002?date={day.isoformat()}").json()["ok"] is True
    page = client.get(f"/api/matches/summary?date={day.isoformat()}").json()
    assert page["total"] == 2 and {item["code"] for item in page["items"]} == {code, "000002"}
    assert page["items"][0]["pool_size"] == scanned["pool_size"] == 2
    assert client.get("/api/matches/summary?date=2020-01-01").status_code == 404

    file = client.get(f"/api/matches/export?date={day.isoformat()}&code={code}")
    assert file.status_code == 200
    assert file.headers["content-type"].startswith("text/csv")
    assert file.headers["X-Export-Rows"] == str(len(scanned["items"]))
    assert file.headers["X-Export-Truncated"] == "0"
    assert file.content.startswith(b"\xef\xbb\xbf"), "接口层负责加 BOM，Excel 才认得中文表头"
    body = file.content.decode("utf-8-sig")
    assert body.splitlines()[0] == ",".join(match_service.CSV_COLUMNS)
    assert len(body.splitlines()) == len(scanned["items"]) + 1
    assert f"matches_{day.isoformat()}_{code}.csv" in file.headers["content-disposition"]

    trimmed = client.get(f"/api/matches/export?date={day.isoformat()}&top=1")
    assert trimmed.headers["X-Export-Truncated"] == "1"
    disposition = trimmed.headers["content-disposition"]
    #: 中文名只许待在 percent-encoded 的 `filename*` 里。老浏览器兜底那一段编不出
    #: latin-1，会让整个导出接口 500（`_截断` 实测踩过），所以它必须纯 ASCII。
    assert "截断" in unquote(disposition.split("filename*=UTF-8''")[1])
    assert disposition.split('filename="')[1].split('"')[0].isascii()
    assert len(trimmed.content.decode("utf-8-sig").splitlines()) - 1 < int(
        trimmed.headers["X-Export-Rows"]
    ) + int(file.headers["X-Export-Rows"])

    top = scanned["items"][0]
    compare = client.get(
        f"/api/matches/compare?code={code}&sample_code={top['code']}"
        f"&date={day.isoformat()}&sample_date={top['limit_date']}"
    ).json()
    assert len(compare["differences"]) == 3 * len(DAILY_SPEC)
    assert compare["score"] == top["score"]
    bad = client.get(
        f"/api/matches/compare?code={code}&sample_code=600000&sample_date=2020-01-01"
    )
    assert bad.status_code == 404 and "没有落库的涨停样本" in bad.json()["detail"]


def test_retention_sweep_clears_derived_rows_that_point_at_deleted_quotes(client):
    """派生数据跟着保留窗口清：样本、明细、训练行都不留在窗口外，原始行情按窗口留着。"""
    days = seed()
    _build_all_patterns(days)
    _scan(days[7])
    _scan(days[10])
    pairs_before = len(_pairs(days[7])) + len(_pairs(days[10]))
    assert pairs_before > 0

    result = retention.purge_expired()
    cutoff = date.fromisoformat(result["cutoff"])
    assert result["retention_trading_days"] == 5
    assert cutoff == days[-5]
    assert [date.fromisoformat(item) for item in result["kept_dates"]] == [
        item for item in days if item >= cutoff
    ]
    for key in (
        "daily_features",
        "limit_up_patterns",
        "pattern_matches",
        "match_summaries",
        "stock_training_rows",
        "skipped_records",
    ):
        assert key in result["deleted"], key
    assert result["deleted"]["daily_quotes"] == 4 * len([item for item in days if item < cutoff])
    assert result["deleted"]["limit_up_patterns"] > 0
    assert result["deleted"]["pattern_matches"] > 0, "样本日已被删掉的明细还留在库里"

    with session_scope() as session:
        kept_quotes = sorted({row.trade_date for row in session.query(DailyQuote)})
        dangling = int(
            session.query(PatternMatch).filter(PatternMatch.sample_limit_date < cutoff).count()
        )
        old_targets = int(session.query(PatternMatch).filter(PatternMatch.target_date < cutoff).count())
        old_patterns = int(
            session.query(LimitUpPattern).filter(LimitUpPattern.limit_up_date < cutoff).count()
        )
        old_skips = int(session.query(SkippedRecord).filter(SkippedRecord.trade_date < cutoff).count())
    assert kept_quotes == [item for item in days if item >= cutoff]
    assert dangling == old_targets == old_patterns == 0
    assert old_skips == 0


def test_the_shared_fixture_is_one_calendar_for_every_judgment(client):
    """夹具的日历必须与库里的行情日期一致：一处改了日历，别处的断言就在讲另一个故事。"""
    days = weekdays(12)
    seed()
    with session_scope() as session:
        assert quote_dates(session) == days
        assert sorted(pattern_service.stock_names(session)) == ["000001", "000002", "600000", "600001"]
    assert len(feature_names()) == VECTOR_DIM
    assert feature_names()[: len(DAILY_SPEC)] == [f"t3_{name}" for name, _group in DAILY_SPEC]
    assert feature_names()[-len(AGGREGATE_NAMES) :] == AGGREGATE_NAMES
