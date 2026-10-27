"""档J：库里「该有的一天」到底谁来补。

用户看见的形状是：行情有 26 号、涨停 56 只，样本页却写着 0 条。断的不是一根线，
是 `daily_quotes` 之下的三层派生数据——过去只有数据管理页那四个按钮会写它们。
所以这份判据盯四件事：

* **缺的日子会被补上**：特征、样本、扫描三层从 0 行长出来，且补完再判一次就没了活；
* **不该动的日子不动**：盘中那半天不算一天，训练表永远不在自动补齐之列；
* **抢不到槽位就老老实实让路**：和手点按钮撞在一起时一条都不写；
* **谁补哪一层分得清**：行情少了去补行情（并钉在已收盘那天），行情齐了只补派生层。

负向控制照常配在每条判据里：补齐后再跑必须无事可做、占位时必须有东西被拦住、
`derive_pass` 抛错时同步消息必须把这句话讲出来而不是变成一个安静的 0。
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time

from sqlalchemy import func, select
from seeds import seed

from backend.app.core.database import session_scope
from backend.app.models.tables import (
    DailyFeature,
    LimitUpPattern,
    MatchSummary,
    StockTrainingRow,
)
from backend.app.services import data_job, freshness
from backend.app.services.sync_service import run_sync_now, set_fetcher


def _evening(day: date) -> datetime:
    """当天收盘之后——这时候那根日线才算落定。"""
    return datetime.combine(day, time(17, 0))


def _morning(day: date) -> datetime:
    return datetime.combine(day, time(10, 0))


def _layer_counts() -> dict[str, int]:
    with session_scope() as session:
        return {
            "features": int(session.scalar(select(func.count(DailyFeature.id))) or 0),
            "patterns": int(session.scalar(select(func.count(LimitUpPattern.id))) or 0),
            "summaries": int(session.scalar(select(func.count(MatchSummary.id))) or 0),
            "training": int(session.scalar(select(func.count(StockTrainingRow.id))) or 0),
        }


# --- 判据一：缺的那几天会被补上 ----------------------------------------------


def test_quotes_without_derived_layers_read_as_missing(client):
    """行情齐、涨停有、派生三层全空——这就是用户截图那一格的形状。"""
    days = seed()
    data_job.reset_state()
    with session_scope() as session:
        verdict = freshness.gaps(session, _evening(days[-1]))
    assert verdict["empty_db"] is False
    assert verdict["quotes_behind"] is False, "行情明明到 12 号，却被判成缺行情"
    assert days[-1] in verdict["features"]
    assert days[9] in verdict["patterns"], "那天有涨停、零样本，却没被列进要补的日子"
    assert verdict["scan"] == days[-1]
    assert freshness.needs_work(verdict) is True


def test_derive_pass_grows_the_three_layers_and_then_has_nothing_to_do(client):
    """补一次就长出来；长出来之后再判必须是「没活」，不然每天开机都在重算全库。"""
    days = seed()
    data_job.reset_state()
    before = _layer_counts()
    assert before == {"features": 0, "patterns": 0, "summaries": 0, "training": 0}

    result = freshness.derive_pass(_evening(days[-1]))
    assert result["ran"] is True
    assert "已自动补齐" in result["note"]
    after = _layer_counts()
    assert after["features"] > 0, "特征层没长出来"
    assert after["patterns"] > 0, "涨停样本层没长出来"
    assert after["summaries"] > 0, "匹配扫描汇总没长出来"

    again = freshness.derive_pass(_evening(days[-1]))
    assert again["ran"] is False, "负向控制失效：补完还判出活，开机就会天天重算"
    assert _layer_counts() == after


def test_derive_pass_leaves_the_training_table_alone(client):
    """训练集是评分模型的输入，悄悄补它等于悄悄换模型——自动补齐必须绕开它。"""
    days = seed()
    data_job.reset_state()
    freshness.derive_pass(_evening(days[-1]))
    assert _layer_counts()["training"] == 0


# --- 判据二：盘中那半天不是一天 ------------------------------------------------


def test_a_partial_day_is_not_counted_as_a_trading_day(client):
    """同一天上午十点判、下午五点判，上限差一个日子。

    上午那半天价格还在动：拿它建样本，三天窗口里就混进半根线，第二天收盘后还得推翻重来。
    """
    days = seed()
    data_job.reset_state()
    with session_scope() as session:
        morning = freshness.gaps(session, _morning(days[-1]))
        evening = freshness.gaps(session, _evening(days[-1]))
    assert morning["settled_day"] == days[-2]
    assert morning["scan"] == days[-2]
    assert days[-1] not in morning["features"], "上午那半天被当成一个完整交易日去建特征了"
    assert days[-1] not in morning["patterns"]
    assert evening["settled_day"] == days[-1]
    assert evening["scan"] == days[-1], "反向对照失败：收盘后判出来的上限也必须是真的那一天"


def test_weekend_and_holiday_rolls_the_target_back_to_a_trading_day(client):
    """`expected_quote_day` 只负责「不在周末要一根线」，节假日判不出来就多跑一次。"""
    assert freshness.expected_quote_day(datetime(2026, 6, 15, 10, 0)) == date(2026, 6, 12)
    assert freshness.expected_quote_day(datetime(2026, 6, 12, 10, 0)) == date(2026, 6, 11)
    assert freshness.expected_quote_day(datetime(2026, 6, 12, 15, 30)) == date(2026, 6, 12)


# --- 判据三：抢不到槽位就让路 --------------------------------------------------


def test_derive_pass_stands_aside_when_a_manual_job_holds_the_slot(client):
    """自动补齐和手点的那个按钮写的是同一批表。撞车的代价是 `database is locked`、两边一起 500，
    所以这一轮必须整条路不走，而不是写一半。"""
    days = seed()
    released = threading.Event()
    started = threading.Event()

    def blocking(session, day, acc):
        started.set()
        assert released.wait(20.0), "测试没把占位线程放出来"

    data_job.reset_state()
    data_job.start("features", dates=[days[-1]], work=blocking)
    assert started.wait(5.0), "占位任务没起来，这条判据就没意义了"
    try:
        before = _layer_counts()
        result = freshness.derive_pass(_evening(days[-1]))
        assert result["ran"] is False
        assert "让路" in result["note"]
        assert _layer_counts() == before, "槽位被占着还写了库"
    finally:
        released.set()
    data_job.reset_state()

    # 槽位空出来之后，同一份活必须立刻补得上——「让路」不是「这轮跳过了就永远不补」。
    result = freshness.derive_pass(_evening(days[-1]))
    assert result["ran"] is True
    assert _layer_counts()["patterns"] > 0


def test_a_hijacked_slot_does_not_borrow_another_runs_numbers(client, monkeypatch):
    """跑完之后槽位可能已经交给下一个任务。那一轮的记账不是我的，数字就不许写进我的消息里。"""
    days = seed()
    data_job.reset_state()
    monkeypatch.setattr(
        data_job,
        "state",
        lambda: {"task_id": "别人的", "result": {"feature_count": 999999, "pattern_count": 888888}},
    )
    result = freshness.derive_pass(_evening(days[-1]))
    assert result["ran"] is True
    assert "顶掉" in result["note"]
    assert "999999" not in result["note"] and "888888" not in result["note"], "把别人那一轮的行数写成了自己的"


# --- 判据四：同步收尾与开机追赶各管哪一层 -------------------------------------


def test_the_slot_names_auto_derive_as_the_occupant(client):
    """自动补齐占的是同一个槽，那它的名字就得出现在 409 与 `running_job` 里。

    用户在数据管理页点按钮时撞上的必须是「自动补齐…正在运行，请等它跑完」，
    而不是一个空占位的名字——那一格就是这条线的存在证明。
    """
    days = seed()
    released = threading.Event()
    started = threading.Event()

    def blocking(session, day, acc):
        started.set()
        assert released.wait(20.0), "测试没把占位线程放出来"

    data_job.reset_state()
    data_job.start("derive", dates=[days[-1]], work=blocking)
    assert started.wait(5.0), "占位任务没起来，这一条判据就没意义了"
    try:
        assert client.get("/api/data/status").json()["running_job"] == "自动补齐特征、涨停样本与匹配扫描"
        answer = client.post("/api/data/rebuild-patterns")
        assert answer.status_code == 409
        assert "自动补齐" in answer.json()["detail"]
    finally:
        released.set()
        data_job.reset_state()


def test_sync_tail_runs_the_derive_pass_and_says_so(client, monkeypatch):
    """同步只写行情那一版，会在页面上留下「这一天有涨停、零样本」。"""
    seed()
    data_job.reset_state()
    seen: list = []
    note = "，已自动补齐 2026-06-12 的特征、样本与扫描（测试注入）"

    def fake_derive(moment: datetime | None = None) -> dict:
        seen.append(moment)
        return {"ran": True, "note": note, "days": []}

    monkeypatch.setattr("backend.app.services.freshness.derive_pass", fake_derive)
    set_fetcher(list_fn=lambda: [], kline_fn=lambda code, start, end: [])
    try:
        result = run_sync_now()
    finally:
        set_fetcher()
    assert seen, "同步收尾没有调用派生补齐"
    assert note in result["message"], "补了什么必须写在完成消息里，不能变成一个安静的 0"


def test_sync_reports_a_failed_derive_pass_instead_of_hiding_it(client, monkeypatch):
    """补齐跑砸了要能在消息里看见。同步本身照样算完成——那三层不该把行情那一层拖成失败。"""

    def boom(moment: datetime | None = None) -> dict:
        raise RuntimeError("样本表写不进去")

    monkeypatch.setattr("backend.app.services.freshness.derive_pass", boom)
    set_fetcher(list_fn=lambda: [], kline_fn=lambda code, start, end: [])
    try:
        result = run_sync_now()
    finally:
        set_fetcher()
    assert result["progress"] == 100
    assert "自动补齐" in result["message"] and "样本表写不进去" in result["message"]


def test_bootstrap_picks_the_layer_that_is_actually_behind(client, monkeypatch):
    """行情缺 → 补行情且钉在已收盘那天；行情齐 → 只补派生层。两条路不重叠。"""
    monkeypatch.setenv("LP_AUTO_SYNC", "1")
    sync_calls: list = []
    derive_calls: list = []
    monkeypatch.setattr(
        "backend.app.services.sync_service.start_sync",
        lambda end=None: sync_calls.append(end) or "task-1",
    )
    monkeypatch.setattr(
        "backend.app.services.freshness.start_derive",
        lambda moment=None: derive_calls.append(moment),
    )
    from backend.app.main import bootstrap_data

    seed()  # 行情到 2026-06-12（周五）
    data_job.reset_state()

    # 周二上午十点开机，昨天（06-15）那根线还没进库：抓的上限是 06-15，不是「今天」那半根线。
    bootstrap_data(datetime(2026, 6, 16, 10, 0))
    assert sync_calls == [date(2026, 6, 15)], "补行情没有钉在已收盘的那天，把开盘两小时的半根线当成了交易日"
    assert derive_calls == [], "补了行情的那条路又另起一个派生线程（同步收尾本来就会补）"

    # 周五上午：上一个交易日（06-11）的线已经在库里，不该再动行情，只该补派生层。
    sync_calls.clear()
    bootstrap_data(datetime(2026, 6, 12, 10, 0))
    assert sync_calls == [], "行情已经覆盖该有的那一天，还是又拉了一遍"
    assert derive_calls == [datetime(2026, 6, 12, 10, 0)]


def test_bootstrap_is_silent_when_auto_sync_is_off(client, monkeypatch):
    """`LP_AUTO_SYNC=0` 是「开机什么都别自己动」那一档，两条路一起关掉。"""
    calls: list = []
    monkeypatch.setenv("LP_AUTO_SYNC", "0")
    monkeypatch.setattr("backend.app.services.sync_service.start_sync", lambda end=None: calls.append(end))
    monkeypatch.setattr("backend.app.services.freshness.start_derive", lambda moment=None: calls.append("derive"))

    from backend.app.main import bootstrap_data

    seed()
    bootstrap_data(datetime(2026, 6, 15, 10, 0))
    assert calls == []
