"""档6 列表页与详情页吃的那两条读数。

两条都不重算任何分：列表页读的是 `score_day` 早先落库的 `match_summaries`，
详情页读的是一根一根日线。所以这里的判据集中在四件事——筛得对不对、排得稳不稳
（同分必须靠代码次关键字，否则翻页会重会漏）、空值落在哪一档、补拉出来的柱子不许
冒充「这根不是涨停」。
"""

from __future__ import annotations

from datetime import date, timedelta

from backend.app.core.database import session_scope
from backend.app.models.tables import CalcBar, DailyQuote, MatchSummary, Stock
from backend.app.services import match_service
from tests.seeds import END, seed, weekdays

DAY = END

#: 每只票一份汇总，七个排序键各不相同，断言才分得开「按哪个排的」。
#: 最后那只刻意做成「一条样本都没比上」：分母 0 时比例和分数都是 NULL，
#: 排序时空值落哪一档才有判据，而不是只靠注释说「SQLite 把 NULL 当最小」。
#:
#: 写入顺序是反着代码来的（600001 先落库、600000 后落）：这两只 `max_similarity` 同为 88.0，
#: 库里那行的物理顺序刚好和代码升序相反。次关键字一旦被删掉，SQLite 就会按物理顺序回
#: `[600001, 600000]`，`test_tie_breaking_keeps_pages_disjoint` 立刻判红——
#: 按代码顺序插入时这条判据是白给的，注入违规都测不出来（我在这上面栽过一次）。
FIXTURE = {
    "600001": {"sample_count": 120, "high_similarity_count": 4, "avg_similarity": 70.25, "max_similarity": 88.0},
    "600000": {"sample_count": 100, "high_similarity_count": 4, "avg_similarity": 61.5, "max_similarity": 88.0},
    "000001": {"sample_count": 100, "high_similarity_count": 25, "avg_similarity": 55.0, "max_similarity": 92.5},
    "000002": {"sample_count": 0, "high_similarity_count": 0, "avg_similarity": None, "max_similarity": None},
}


def _write_summaries(day: date = DAY) -> None:
    with session_scope() as session:
        for code, fields in FIXTURE.items():
            session.add(
                MatchSummary(
                    target_code=code,
                    target_date=day,
                    pool_dates=[day.isoformat()],
                    pool_size=fields["sample_count"],
                    band="80~90" if (fields["max_similarity"] or 0) >= 80 else "<60",
                    **fields,
                )
            )


def _board(code: str, value: str) -> None:
    """把某只票挪到另一个交易所板块：列表页那个板块下拉吃的就是 `stocks.board`。"""
    with session_scope() as session:
        row = session.get(Stock, code)
        assert row is not None
        row.board = value


def _page(client, **params) -> dict:
    text = "&".join(f"{key}={value}" for key, value in params.items())
    response = client.get(f"/api/matches/summary?date={DAY.isoformat()}&{text}")
    assert response.status_code == 200, response.text
    return response.json()


def _codes(page: dict) -> list[str]:
    return [item["code"] for item in page["items"]]


def test_list_seeded(client):
    """夹具本身：四只票一天各一份汇总，`total` 数的是筛完之后的条数。"""
    seed()
    _write_summaries()
    page = _page(client, limit=100)
    assert page["trade_date"] == DAY.isoformat()
    assert page["total"] == 4
    assert len(_codes(page)) == 4
    assert page["sort"] == "max" and page["order"] == "desc"
    #: 板块两列一起回：`board` 给筛选与链接用，`board_label` 给页面直接画。
    assert {item["board"] for item in page["items"]} == {"main_board"}
    assert {item["board_label"] for item in page["items"]} == {"主板"}


def test_every_declared_sort_key_is_honoured(client):
    """七个排序键逐个验：读回包自己那一列，要求它按方向单调，且第一个就是极值持有者。

    单调性从**回包里的值**现推，判据不依赖我把期望顺序抄对——抄错那次就是负向控制本身：
    `sort=avg` 若实际还在按 `max` 排，`avg` 那一列立刻不单调。
    """
    seed()
    _write_summaries()
    column = {
        "max": "max_similarity",
        "avg": "avg_similarity",
        "rate": "high_similarity_rate",
        "count": "high_similarity_count",
        "samples": "sample_count",
        "code": "code",
        "name": "name",
    }
    assert set(column) == set(match_service.SUMMARY_SORTS)
    for key in match_service.SUMMARY_SORTS:
        for order in ("desc", "asc"):
            page = _page(client, limit=100, sort=key, order=order)
            values = [
                (item["code"] if key == "code" else item["name"] if key == "name" else item[column[key]])
                for item in page["items"]
            ]
            present = [value for value in values if value is not None]
            assert all(
                (a >= b if order == "desc" else a <= b) for a, b in zip(present, present[1:])
            ), f"{key}/{order} 没排出来：{values}"
            #: 分母为 0 或库里就没这个数时那一列是 None。SQLite 把 NULL 当最小，
            #: 所以倒序时空值垫在最后、升序时排在最前——这是实测出来的位置，不是猜的。
            nulls = [index for index, value in enumerate(values) if value is None]
            if nulls:
                if order == "desc":
                    assert max(nulls) == len(values) - 1, f"{key}/desc 空值没垫底：{values}"
                else:
                    assert min(nulls) == 0, f"{key}/asc 空值没置顶：{values}"


def test_tie_breaking_keeps_pages_disjoint(client):
    """`max` 有两只同为 88.0：次关键字必须是代码，且翻页不重不漏。"""
    seed()
    _write_summaries()
    page = _page(client, limit=100, sort="max", order="desc")
    top = [item for item in page["items"] if item["max_similarity"] == 88.0]
    assert [item["code"] for item in top] == sorted(item["code"] for item in top)
    #: 完整顺序照服务层那段声明抄：极值在前、同分按代码升序、空值垫底。
    assert _codes(page) == ["000001", "600000", "600001", "000002"]
    assert _codes(_page(client, limit=100, sort="max", order="asc")) == [
        "000002",
        "600000",
        "600001",
        "000001",
    ]

    seen: list[str] = []
    for offset in range(4):
        one = _page(client, limit=1, offset=offset, sort="max", order="desc")
        assert one["total"] == 4
        seen += _codes(one)
    assert len(seen) == 4 and len(set(seen)) == 4
    assert seen == [item["code"] for item in _page(client, limit=100, sort="max", order="desc")["items"]]


def test_keyword_and_board_filters(client):
    """代码前缀与名称都能筛；板块下拉的条数跟着关键词走、不跟着板块自己走。"""
    seed()
    _write_summaries()
    _board("000002", "chinext")

    by_name = _page(client, limit=100, q="浦发")
    assert _codes(by_name) == ["600000"]
    assert by_name["total"] == 1 and by_name["q"] == "浦发"

    #: `%0000%` 是子串匹配，`600000`/`600001` 中间也带着这四个 0。
    #: 这里写全四只，是防止哪天把关键词筛「优化」成前缀匹配却没人发现。
    by_code = _page(client, limit=100, q="0000")
    assert sorted(_codes(by_code)) == ["000001", "000002", "600000", "600001"]
    assert by_code["total"] == 4

    chinext = _page(client, limit=100, board="chinext")
    assert _codes(chinext) == ["000002"]
    #: 中文名从后端出：前端那份映射是第二把尺子，后端加一档（北交所）它就会漏。
    assert chinext["items"][0]["board"] == "chinext"
    assert chinext["items"][0]["board_label"] == "创业板"
    #: 负向控制：选了创业板之后，下拉里主板那档必须还在、还带着自己的条数。
    #: 如果 boards 跟着 board 过滤走，这里会变成只剩一条，用户想换回去就只能先清空选择。
    boards = {item["board"]: item for item in chinext["boards"]}
    assert set(boards) == {"main_board", "chinext"}
    assert boards["main_board"]["count"] == 3 and boards["chinext"]["count"] == 1
    assert boards["main_board"]["label"] == "主板"

    filtered = _page(client, limit=100, q="0000", board="chinext")
    assert _codes(filtered) == ["000002"]
    assert {item["board"] for item in filtered["boards"]} == {"main_board", "chinext"}


def test_bad_sort_or_order_is_refused_with_the_allowed_list(client):
    seed()
    _write_summaries()
    refused = client.get("/api/matches/summary?sort=nope")
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert "不支持的排序字段" in detail and "max" in detail and "rate" in detail

    refused = client.get("/api/matches/summary?order=sideways")
    assert refused.status_code == 422
    assert "desc" in refused.json()["detail"]

    #: 服务层那道闸是给绕过路由的调用方兜底的，不是只长在接口上。
    with session_scope() as session:
        try:
            match_service.summary_page(session, DAY, sort="nope")
        except ValueError as exc:
            assert "不支持的排序字段" in str(exc)
        else:  # pragma: no cover - 判据就是要它进不了排序
            raise AssertionError("服务层没挡住非法排序键")


def test_missing_day_is_404_and_limit_bounds_hold(client):
    seed()
    _write_summaries()
    missing = client.get("/api/matches/summary?date=2020-01-01")
    assert missing.status_code == 404
    assert "这一天没有行情" in missing.json()["detail"]
    assert client.get("/api/matches/summary?limit=0").status_code == 422
    assert client.get("/api/matches/summary?limit=1001").status_code == 422


def test_kline_marks_limit_up_only_for_official_quotes(client):
    """详情页那排柱子：正式行情那几根带真布尔值，补拉的计算柱是 null 而不是 false。

    「这根不是涨停」和「这根是算出来的、判不了」是两个事实，读成同一个数就等于
    让页面替库里没有的判定下结论。
    """
    days = seed()
    #: 期望值从库里抄，不从我对夹具的记忆里抄：`UNIVERSE` 那行改了涨停分布，
    #: 这条判据要跟着变，而不是先把「我以为只有三天」写死再判红。
    with session_scope() as session:
        expected = {
            row.trade_date.isoformat()
            for row in session.query(DailyQuote)
            .filter(DailyQuote.stock_code == "600000", DailyQuote.is_limit_up.is_(True))
            .all()
        }
    assert expected == {days[index].isoformat() for index in (4, 7, 9)}

    payload = client.get("/api/kline?code=600000&days=60").json()
    bars = {item["date"]: item for item in payload["bars"]}
    assert {key for key, value in bars.items() if value["is_limit_up"] is True} == expected
    others = [
        value["is_limit_up"]
        for key, value in bars.items()
        if key not in expected
    ]
    assert all(item is False for item in others), others

    #: 造一根只有计算线的日子：库里没有它的正式行情，`is_limit_up` 必须是 null。
    ghost = days[0] - timedelta(days=3)
    while ghost.weekday() >= 5:
        ghost -= timedelta(days=1)
    assert ghost not in days and ghost not in set(weekdays(1))
    with session_scope() as session:
        session.add(
            CalcBar(
                stock_code="600000",
                trade_date=ghost,
                open=9.0,
                high=9.5,
                low=8.8,
                close=9.4,
                pre_close=9.0,
                volume=1000,
                amount=9400,
            )
        )
    payload = client.get("/api/kline?code=600000&days=60").json()
    bars = {item["date"]: item for item in payload["bars"]}
    assert bars[ghost.isoformat()]["is_limit_up"] is None
    #: 同一份回包里三种值都出现过：True / False / null，页面才能把它们分开画。
    assert {value["is_limit_up"] for value in bars.values()} == {True, False, None}


def test_kline_header_carries_the_stock_profile(client):
    seed()
    _board("000001", "chinext")
    payload = client.get("/api/kline?code=000001&days=5").json()
    assert payload["code"] == "000001"
    assert payload["name"] == "平安银行"
    assert payload["board"] == "chinext"
    assert payload["board_label"] == "创业板"
    assert payload["is_st"] is False
    unknown = client.get("/api/kline?code=600999&days=5").json()
    assert unknown["board"] == "" and unknown["board_label"] == "" and unknown["bars"] == []
