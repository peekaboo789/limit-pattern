"""盘中实时监控 + 三条付费 AI 链路。

网关那一层用**本机上的一个假 SSE 服务**打真请求：`httpx` 的流式解析、usage 归并、
`response_format` 降级重发、空正文那一句诚实的失败，只有真跑一遍 HTTP 才测得到；
把 `call_model` 整个换成返回值，等于这些病随时可以在下一次改动里复发。

真网关一次都不敲：那是要按 token 付钱的，用户没点头就不能替他花。
所以这里出现的 `sk-test-…` 是编出来专门验证「密钥不会漏进报告」的假串，不是任何真实凭据。
"""

import json
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.app.core.database import session_scope
from backend.app.models.tables import DailyQuote, Stock, TradingDay
from backend.app.services import ai_picks
from backend.app.services.limit_up_service import annotate_history, limit_up_price, ratio_for
from backend.app.services.score_model import read_composite

SECRET = "sk-test-secret-123456"


# --- 造数据 ------------------------------------------------------------------


def weekdays_back(count: int, end: date) -> list[date]:
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
                "amount": 10000 + index * 100,
                "turnover_rate": 1.2 + index / 10,
            }
        )
        price = close
    return rows


def _save(code: str, name: str, exchange: str, rows: list[dict]) -> None:
    board = "main_board"
    labelled = annotate_history(
        rows, board=board, is_st=False, list_date=None, trading_days=[row["trade_date"] for row in rows]
    )
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


#: 样本池里必须有**别人**的涨停：一只股票如果只和自己那一天像，`_score` 会把它整行剔掉
#: （涨停那天已在样本池里的不能跟自己比），榜上就少一行。所以两只都在倒数第二天封一次，
#: 再在最后一天（种子日）各封一次；600001 今天没封，当 pending 行。
def _seed(last_day: date) -> list[date]:
    days = weekdays_back(9, last_day)
    tail = len(days) - 1
    _save("600000", "浦发银行", "SSE", _series(days, limit_on={tail - 1, tail}))
    _save("000002", "万科A", "SZSE", _series(days, limit_on={tail - 1, tail}, start=9.0))
    _save("600001", "某只观察", "SSE", _series(days, limit_on=set(), start=7.0))
    return days


# --- 假网关 ------------------------------------------------------------------


class _Gateway(ThreadingHTTPServer):
    """一个真的 HTTP 服务：把 `script` 里的内容按 SSE 一帧一帧吐回去。

    `requests` 记下每一次请求体，测试据此断言「没配置就没敲网关」「降级只重发一次」
    「两次 POST 只花一份钱」。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, script):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = script
        self.requests: list[dict] = []
        self._stopped = False
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.base_url = f"http://{self.server_address[0]}:{self.server_address[1]}/v1"

    def start(self) -> "_Gateway":
        self.thread.start()
        return self

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # 别把测试输出刷成一屏访问日志
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            body = {}
        self.server.requests.append(body)
        outcome = self.server.script(body, len(self.server.requests))
        if outcome[0] == "status":
            _status, code, message = outcome[:3]
            #: 上游说「稍后再试」时会带 `Retry-After`；测试要能钉住它，
            #: 否则退避表会让每一次重试都真睡 5 秒、15 秒。
            extra = outcome[3] if len(outcome) > 3 else {}
            payload = json.dumps({"error": {"message": message}}, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            for key, value in extra.items():
                self.send_header(key, str(value))
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return
        _kind, pieces = outcome
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in pieces:
            self.wfile.write(f"data: {json.dumps(piece, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


def sse_script(content_pieces: list[str], *, usage: dict | None = None, finish: str = "stop",
               thinking_pieces: list[str] | None = None):
    """思考帧先来、正文按 `content_pieces` 分帧来，最后一帧才带 usage。

    `thinking_pieces` 单独一组：推理模型把思考放在 `delta.reasoning_content`，
    那部分一个字都不许并进正文，但字数要能对上——这两件事都要有正向对照才测得住。
    """

    def script(_body: dict, _n: int):
        pieces = [{"choices": [{"delta": {"reasoning_content": piece}}]} for piece in thinking_pieces or []]
        pieces += [{"choices": [{"delta": {"content": piece}, "finish_reason": None}]} for piece in content_pieces]
        pieces.append({"choices": [{"delta": {}, "finish_reason": finish}], "usage": usage or {}})
        return ("sse", pieces)

    return script


def reject_format_then_answer(answer: str):
    """第一趟报「不认 response_format」，第二趟才回话——降级重发那条路的正身。"""

    def script(body: dict, n: int):
        if n == 1:
            return ("status", 400, "Unsupported parameter: 'response_format' is not supported by this model")
        return ("sse", [{"choices": [{"delta": {"content": answer}, "finish_reason": "stop"}]}])

    return script


def empty_answer_script(thinking_chars: int = 512):
    def script(_body: dict, _n: int):
        pieces = [{"choices": [{"delta": {"reasoning_content": "想" * thinking_chars}}]}]
        pieces.append({"choices": [{"delta": {"content": ""}, "finish_reason": "length"}]})
        return ("sse", pieces)

    return script


CONCURRENCY_429 = (
    '{"detail":{"error":{"message":"Model \'test-model\' is at its concurrency limit (16); '
    'please retry later or use another model","type":"rate_limit_error",'
    '"code":"model_concurrency_rate_limit_exceeded"}}}'
)


def reject_then_answer(code: int, message: str, answer: str, *, times: int = 1):
    """前 `times` 趟被上游挡在做结论之前（带 `Retry-After: 0` 让测试不真睡），之后正常回话。"""

    def script(body: dict, n: int):
        if n <= times:
            return ("status", code, message, {"Retry-After": "0"})
        return ("sse", [{"choices": [{"delta": {"content": answer}, "finish_reason": "stop"}]}])

    return script


def always_reject(code: int, message: str):
    """每一趟都被同样挡回来——重试上限与「这趟没花钱」那一句只有这样才测得住。"""

    def script(_body: dict, _n: int):
        return ("status", code, message, {"Retry-After": "0"})

    return script


# --- 夹具 -------------------------------------------------------------------


@pytest.fixture()
def ai_client(client, monkeypatch):
    """盘后回放 + 假网关 + 配好三项设置的客户端。

    两件必须钉死的事：

    1. `_mode` 钉在 `settled`。测试机器上此刻可能真是交易时段，那一档会去敲真行情源，
       榜上的价就成了别人家的数。
    2. `clock.today()` 与 `routes.today()` 一起钉到种子最后一天。固定榜有一道
       「交易日必须是今天」的闸，而周末跑测试时种子日只能落在周五——不钉住，
       那道闸会把测试自己的排期当成 bug 报出来。
    """
    from backend.app.api import routes
    from backend.app.core import clock
    from backend.app.services import ai_analysis_service, ai_daily_rank_service, realtime_service
    from backend.app.services.realtime_tracker import RealtimeTracker, tracker

    for name in ("AI_BASE_URL", "AI_MODEL", "AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    last_day = date.today()
    if last_day.weekday() >= 5:  # 周末就往前挪到周五，交易日闸才过得去
        last_day -= timedelta(days=last_day.weekday() - 4)
    days = _seed(last_day)
    monkeypatch.setattr(clock, "today", lambda: last_day)
    monkeypatch.setattr(routes, "today", lambda: last_day)
    monkeypatch.setattr(RealtimeTracker, "_mode", lambda self: realtime_service.MODE_SETTLED)
    monkeypatch.setattr(ai_analysis_service, "in_session", lambda: False)

    servers: list[_Gateway] = []

    def configure(script):
        server = _Gateway(script).start()
        servers.append(server)
        client.put(
            "/api/settings",
            json={"ai_base_url": server.base_url, "ai_model": "test-model", "ai_api_key": SECRET},
        )
        return server

    # 榜要等后台线程第一轮全市场跑完才有成员，`top()` 第一拍回的是 warming。
    # 这里等的是那一份真榜，不是「重进页面碰运气」。
    deadline = time.monotonic() + 30.0
    warmed = None
    while time.monotonic() < deadline:
        warmed = client.get("/api/realtime/top").json()
        if warmed.get("status") == "live" and warmed.get("snapshot"):
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"监控榜在 30 秒里没预热成 live：{warmed}")

    yield {"client": client, "days": days, "last_day": last_day, "configure": configure, "board": warmed["snapshot"]}

    for server in servers:
        server.stop()
    tracker.stop()  # 让后台线程在下一拍自己退出，别把状态带进下一个临时库
    thread = tracker._thread
    if thread is not None:
        until = time.monotonic() + 5.0
        while thread.is_alive() and time.monotonic() < until:
            time.sleep(0.05)
    tracker.reset()
    realtime_service.clear_samples_cache()
    realtime_service.set_spot_fetcher()
    ai_analysis_service.board_runner.reset()
    ai_analysis_service.single_runner.reset()
    ai_daily_rank_service.daily_rank_runner.reset()


def _wait_window_job(client, *, timeout: float = 60.0) -> None:
    """改窗口会起一个后台重建任务：等它收工，不然断言读到的还是旧口径那一轮。"""
    deadline = time.monotonic() + timeout
    while client.get("/api/data/job").json()["running"]:
        assert time.monotonic() < deadline, "改窗口的后台任务没在期限内收工"
        time.sleep(0.05)


def wait_for(client, path: str, *, timeout: float = 30.0) -> dict:
    """轮询到那一份跑完。等的是后台线程，不是重进页面。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(path).json()
        if not payload.get("runner", {}).get("running"):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"{path} 在 {timeout} 秒里没跑完")


def wait_for_requests(server, count: int, *, timeout: float = 20.0) -> None:
    """等假网关真的收到第 N 次请求——「两次 POST 只花一份钱」要卡在请求已发出、还没回话那一瞬。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(server.requests) >= count:
            return
        time.sleep(0.02)
    raise AssertionError(f"网关只收到 {len(server.requests)} 次请求，等的是 {count} 次")


# --- 实时监控 ----------------------------------------------------------------


def test_realtime_board_is_replayed_and_scores(ai_client):
    payload = ai_client["client"].get("/api/realtime/top").json()
    assert payload["status"] == "live", payload
    snapshot = payload["snapshot"]
    assert snapshot["window_mode"] == "settled"
    assert snapshot["pool_size"] >= 1
    assert snapshot["trade_date"] == ai_client["last_day"].isoformat()
    codes = {row["code"] for row in snapshot["limit_up_rows"]} | {row["code"] for row in snapshot["pending_rows"]}
    assert {"600000", "600001"} <= codes
    for row in snapshot["limit_up_rows"]:
        assert row["is_limit_up"] and row["to_limit_percent"] == 0.0
    for row in snapshot["pending_rows"]:
        assert not row["is_limit_up"] and row["to_limit_percent"] > 0.0
    # 每行都带着三项分量与「比过几条」的出处，页面上那一列才有账可对
    for row in snapshot["limit_up_rows"]:
        assert {"shape_score", "volume_score", "volume_pct_score", "avg_score", "sample_count"} <= set(row)
        assert row["score"] is not None


def test_realtime_status_is_cheap_and_honest(ai_client):
    status = ai_client["client"].get("/api/realtime/status").json()
    assert status["refresh_seconds"] == 2
    assert status["full_round_seconds"] == 120
    assert status["top_n_min"] <= status["top_n_default"] <= status["top_n_max"]
    assert status["window_mode"] == "settled"
    assert {item["key"] for item in status["metrics"]} == {"score", "shape", "volume", "volume_pct", "gap"}
    assert "morning_freeze" in status


def test_metric_switch_uses_the_same_scored_round(ai_client):
    """换口径 = 换成员，而且成员只能来自上一轮全市场那份打分结果。

    只重排现有 N 行是骗人的榜：量能分最高的那几只本来就可能没进按综合分选出的前 N。
    """
    client = ai_client["client"]
    by_score = client.get("/api/realtime/top", params={"metric": "score"}).json()
    assert by_score["metric"] == "score" and by_score["snapshot"]["row_count"] == 3
    by_gap = client.get("/api/realtime/top", params={"metric": "gap"}).json()
    assert by_gap["metric"] == "gap"
    assert by_gap["status"] == "live"
    assert by_gap["snapshot"]["row_count"] == 3
    gap_values = [
        row["to_limit_percent"]
        for row in by_gap["snapshot"]["limit_up_rows"] + by_gap["snapshot"]["pending_rows"]
    ]
    assert gap_values == sorted(gap_values)


def test_realtime_rejects_out_of_range_selection(ai_client):
    client = ai_client["client"]
    assert client.get("/api/realtime/top", params={"metric": "nope"}).status_code == 422
    assert client.get("/api/realtime/top", params={"top_n": 3}).status_code == 422


# --- 网关层（真 HTTP）--------------------------------------------------------


def test_gateway_streams_and_reports_usage(ai_client):
    """流式拼接 + usage 归并：正文分四帧来，最后一帧才带 usage。"""
    from backend.app.services import ai_gateway

    server = ai_client["configure"](
        sse_script(['{"criteria":"c",', '"picks":', '[{"stock_code":"600000","score":70.0,', '"reason":"r"}]}'])
    )
    content, usage = ai_gateway.call_model([{"role": "user", "content": "x"}])
    assert json.loads(content)["picks"][0]["stock_code"] == "600000"
    assert usage["finish_reason"] == "stop"
    assert usage["prompt_tokens"] is None  # 上游没给就留 null，不补 0
    assert len(server.requests) == 1


def test_gateway_never_sends_max_tokens_or_thinking(ai_client):
    from backend.app.services import ai_gateway

    server = ai_client["configure"](sse_script(["正文"], thinking_pieces=["思考一下"]))
    content, _usage = ai_gateway.call_model([{"role": "user", "content": "x"}])
    body = server.requests[0]
    assert "max_tokens" not in body and "thinking" not in json.dumps(body)
    assert body["stream"] is True
    assert body["response_format"] == {"type": "json_object"}
    assert content == "正文"  # 思考那一帧一个字都不许并进正文


def test_gateway_progress_counts_answer_and_thinking_on_one_ruler(ai_client):
    """进度回调的阈值必须拿「正文 + 思考」这个总量比。

    负向对照就藏在这一句里：先前的写法拿 `(正文 or 思考)` 跟「两者之和」那个水位比，
    报过一次之后就再也不报——页面会停在「已收到 250 字」假装卡死。
    """
    from backend.app.services import ai_gateway

    reports: list[tuple[int, int]] = []
    server = ai_client["configure"](
        sse_script(["0123456789"] * 50, thinking_pieces=["想" * 250])
    )
    ai_gateway.call_model([{"role": "user", "content": "x"}], on_progress=lambda a, t: reports.append((a, t)))
    assert reports, "一个字都没报给页面"
    assert reports[0][1] == 250 and reports[0][0] == 0  # 思考先到账，正文还没开始
    assert len(reports) >= 3 and reports[-1][0] > 0
    totals = [answer + thinking for answer, thinking in reports]
    assert all(a > b for a, b in zip(totals[1:], totals[:-1]))  # 每报一次就比上一次多一截
    del server


def test_ai_analysis_downgrades_response_format_once(ai_client):
    """降级重发住在调用方那一层：`call_model` 只负责把「上游不认这个参数」翻译成一句话。"""
    client = ai_client["client"]
    answer = json.dumps({"criteria": "c", "picks": [{"stock_code": "600000", "score": 70, "reason": "r"}]})
    server = ai_client["configure"](reject_format_then_answer(answer))
    client.post("/api/ai/analysis")
    report = wait_for(client, "/api/ai/analysis")["report"]
    assert report["ok"] is True and report["response_format"] is False
    assert len(server.requests) == 2
    assert "response_format" in server.requests[0]
    assert "response_format" not in server.requests[1]
    # 两次只该差那一个参数：提示词本身重拼一遍就是两份不同的输入
    assert server.requests[0]["messages"] == server.requests[1]["messages"]


def test_gateway_empty_body_is_an_honest_failure(ai_client):
    from backend.app.services import ai_gateway

    server = ai_client["configure"](empty_answer_script())
    with pytest.raises(ai_gateway.AiGatewayError) as caught:
        ai_gateway.call_model([{"role": "user", "content": "x"}])
    message = str(caught.value)
    assert "正文是空的" in message and "finish_reason=length" in message and "思考 512 字" in message
    assert caught.value.sent is True
    del server


def test_gateway_scrubs_the_key_out_of_failures(ai_client):
    """上游把请求头里的密钥回显在错误文案里时，报告里必须看不见它。"""
    from backend.app.services import ai_gateway

    server = ai_client["configure"](lambda _b, _n: ("status", 401, f"invalid api key {SECRET}"))
    with pytest.raises(ai_gateway.AiGatewayError) as caught:
        ai_gateway.call_model([{"role": "user", "content": f"x {SECRET}"}])
    assert SECRET not in str(caught.value)
    assert "***" in str(caught.value)
    del server


def test_unconfigured_ai_is_refused_before_any_request(ai_client):
    """三件套没配齐 = 一次请求都不发，也不是 500。

    那句话必须点名「当前生效的是哪一条网关」并列出缺的哪几样：清单能切到自建那一条，
    只说「去填设置页」会把人支到另一条网关上，补齐了照样 401。
    """
    from backend.app.services import ai_gateway

    client = ai_client["client"]
    assert ai_gateway.ai_ready() is False
    with pytest.raises(ai_gateway.AiNotConfigured):
        ai_gateway.call_model([{"role": "user", "content": "x"}])
    refused = client.post("/api/ai/analysis")
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "设置页里那三项" in detail
    assert "缺的是网关地址、模型名、密钥" in detail
    assert "一分钱都没花" in detail


# --- 盘中整榜分析 ------------------------------------------------------------


def test_board_analysis_full_cycle(ai_client):
    """一次 POST → 轮询 → 结论：池外代码剔掉、没给分留 null、差值只算一处、花钱落档。

    第二次 POST 卡在第一趟还压在网关上那一刻：非阻塞抢槽位必须说「那一份还在跑」，
    而不是排队再花一份钱。
    """
    client = ai_client["client"]
    board = client.get("/api/realtime/top").json()["snapshot"]
    codes = [row["code"] for row in board["limit_up_rows"] + board["pending_rows"]]
    answer = json.dumps(
        {
            "criteria": "我看的是量能与形态是否同时到位",
            "picks": [
                {"stock_code": codes[0], "score": 88.5, "reason": "封板整齐，量能也跟上", "counter_evidence": "只有三天样本"},
                {"stock_code": "999999", "score": 99.0, "reason": "编的"},
                {"stock_code": codes[1], "reason": "没给分的那一条"},
            ],
        },
        ensure_ascii=False,
    )
    release = threading.Event()

    def slow_script(body, n):
        release.wait(30)
        return sse_script([answer], usage={"prompt_tokens": 900, "completion_tokens": 30})(body, n)

    server = ai_client["configure"](slow_script)
    started = client.post("/api/ai/analysis").json()
    assert started["accepted"] is True
    wait_for_requests(server, 1)

    # 等网关这段时间不该占着连接：库里还读得动，说明页面上的榜和别的接口没被这次分析堵住
    assert client.get("/api/health").status_code == 200
    second = client.post("/api/ai/analysis").json()
    assert second["accepted"] is False and second["already_running"] is True

    release.set()
    state = wait_for(client, "/api/ai/analysis")
    report = state["report"]
    assert report["ok"] is True and report["request_sent"] is True
    assert report["status"] == "ok" and report["chain"] == "realtime_board"
    assert len(server.requests) == 1  # 两次 POST 只花一份钱
    # 只认池内代码：池外那条被丢掉并计数，页面上不会冒出一只没在榜上的股票
    picked = [pick["stock_code"] for pick in report["picks"]]
    assert codes[0] in picked and "999999" not in picked
    assert report["dropped_codes"] == ["999999"]
    # 模型没给分 = null，不是 0
    un_scored = [pick for pick in report["picks"] if pick["score"] is None]
    assert len(un_scored) == 1
    for pick in report["picks"]:
        formula = pick["evidence"]["formula"]
        composite = read_composite(formula)
        if pick["score"] is None:
            assert formula["model_gap"] is None and formula["model_score"] is None
        elif composite is None:
            assert formula["model_gap"] is None
        else:
            assert formula["model_gap"] == pytest.approx(round(pick["score"] - composite, 2), abs=0.001)
    assert report["criteria"].startswith("我看的是")
    assert report["cost_text"] == "输入 900 tokens · 输出 30 tokens"
    assert report["report_id"] and report["store_error"] == ""
    assert report["feed_rows"] == 3
    assert SECRET not in json.dumps(report, ensure_ascii=False)


def test_board_analysis_archives_and_reads_back(ai_client):
    client = ai_client["client"]
    answer = json.dumps({"criteria": "c", "picks": [{"stock_code": "600000", "score": 70, "reason": "r"}]})
    server = ai_client["configure"](sse_script([answer]))
    client.post("/api/ai/analysis")
    state = wait_for(client, "/api/ai/analysis")
    report_id = state["report"]["report_id"]
    assert state["source"] == "memory"
    listed = client.get("/api/ai/reports", params={"chain": "realtime_board"}).json()
    assert listed["items"][0]["report_id"] == report_id
    assert listed["items"][0]["recommended_codes"] == ["600000"]
    detail = client.get(f"/api/ai/reports/{report_id}").json()
    assert detail["report_id"] == report_id and detail["chain"] == "realtime_board"
    assert detail["picks"][0]["stock_code"] == "600000"
    assert detail["archived_at"]
    assert client.get("/api/ai/reports/99999").status_code == 404
    del server


def test_board_analysis_failure_becomes_a_report_not_a_500(ai_client):
    client = ai_client["client"]
    server = ai_client["configure"](always_reject(503, "upstream busy"))
    client.post("/api/ai/analysis")
    report = wait_for(client, "/api/ai/analysis")["report"]
    assert report["ok"] is False and report["status"] == "failed"
    assert "HTTP 503" in report["error"]
    assert report["request_sent"] is True
    #: 5xx 是「做出结论之前就被挡回来」：请求真发出去了，但一个 token 都没生成
    assert report["billed"] is False
    assert report["picks"] == [] and report["cost_text"] == ""
    del server


def test_a_transient_rejection_is_retried_and_costs_nothing_extra(ai_client):
    """上游说「并发满了，请稍后再试」：等一拍重发是免费的，也正是它要求的做法。"""
    from backend.app.core.constants import AI_TRANSIENT_RETRY_ATTEMPTS

    client = ai_client["client"]
    answer = json.dumps({"criteria": "c", "picks": [{"stock_code": "600000", "score": 88.0, "reason": "r"}]})
    server = ai_client["configure"](reject_then_answer(429, CONCURRENCY_429, answer, times=1))
    client.post("/api/ai/analysis")
    report = wait_for(client, "/api/ai/analysis")["report"]
    assert report["ok"] is True, report["error"]
    assert len(server.requests) == 2  # 第一次挡回、第二次成：只多敲了一趟，没多花一份结论的钱
    assert report["request_sent"] is True and report["billed"] is True
    assert report["picks"][0]["stock_code"] == "600000"
    del server
    assert AI_TRANSIENT_RETRY_ATTEMPTS >= 2


def test_a_never_clearing_rejection_spends_nothing_and_says_so(ai_client):
    """重试到上限还是被挡：这一趟必须写成「发出去了、没花钱」，不能靠状态倒推成「没发请求」。"""
    from backend.app.core.constants import AI_TRANSIENT_RETRY_ATTEMPTS

    client = ai_client["client"]
    server = ai_client["configure"](always_reject(429, CONCURRENCY_429))
    client.post("/api/ai/analysis")
    report = wait_for(client, "/api/ai/analysis")["report"]
    assert report["ok"] is False
    assert len(server.requests) == AI_TRANSIENT_RETRY_ATTEMPTS
    assert "HTTP 429" in report["error"] and "没花钱" in report["error"]
    assert report["request_sent"] is True and report["billed"] is False
    assert report["picks"] == []
    del server


def test_daily_rank_429_books_the_money_honestly(ai_client):
    """固定榜那一趟被 429 挡回来：库里、槽位、页面上三处都得说「发了、没花」。"""
    client = ai_client["client"]
    server = ai_client["configure"](always_reject(429, CONCURRENCY_429))
    assert client.post("/api/ai/daily-rank/prepare").json()["ok"] is True
    assert len(server.requests) == 0  # 备输入一分钱没花，跟这次失败无关
    assert client.post("/api/ai/daily-rank/rank").json()["accepted"] is True
    state = wait_for(client, "/api/ai/daily-rank")
    board = state["board"]
    assert board["status"] == "failed" and "HTTP 429" in board["error"]
    assert board["picks_count"] == 0
    assert board["result"]["request_sent"] is True and board["result"]["billed"] is False
    last = state["runner"]["last"]
    assert last["request_sent"] is True and last["billed"] is False
    del server


def test_board_analysis_records_the_ledger(ai_client):
    """推荐出去的那几只要进对账账本，并且从没定判的不进命中率分母。"""
    client = ai_client["client"]
    answer = json.dumps({"criteria": "c", "picks": [{"stock_code": "600000", "score": 70, "reason": "r"}]})
    server = ai_client["configure"](sse_script([answer]))
    client.post("/api/ai/analysis")
    wait_for(client, "/api/ai/analysis")
    with session_scope() as session:
        before = ai_picks.summary(session)["check"]
        assert before["total"] == 1
        assert before["pending"] == 1 and before["settled"] == 0
        assert before["hit_rate_percent"] is None
        checked = ai_picks.reconcile(session)
    assert checked["judged"] == 0 and checked["still_pending"] == 1
    summary = client.get("/api/ai/picks/summary").json()
    assert summary["check"]["hit_rate_percent"] is None
    assert summary["verdict_rule"] and summary["formula"]  # 口径行由后端回传，页面不另写一套
    never = client.get("/api/ai/picks/600999").json()
    assert never["never_recommended"] is True and never["history"] is None
    del server


def test_analysis_on_an_empty_board_spends_nothing(ai_client):
    """榜一行证据都凑不出来时：说清是哪一步空、钱没花、并且不往历史里塞开机误点。"""
    from backend.app.services import ai_analysis_service

    client = ai_client["client"]
    server = ai_client["configure"](sse_script(['{"picks":[]}']))

    def empty_board() -> dict:
        return {"trade_date": ai_client["last_day"].isoformat(), "base_date": "", "limit_up_rows": [], "pending_rows": []}

    report = ai_analysis_service.board_runner.run_once("", empty_board, selection={"metric": "score"})
    assert report["ok"] is False and report["request_sent"] is False
    assert "没有任何可分析的行" in report["error"]
    assert report["report_id"] is None and len(server.requests) == 0
    assert client.get("/api/ai/reports").json()["items"] == []


# --- 单股直评 ----------------------------------------------------------------


def test_single_stock_scores_only_that_one(ai_client):
    client = ai_client["client"]
    answer = json.dumps({"criteria": "只看这一只", "picks": [{"stock_code": "600001", "score": 66.0, "reason": "还在观察"}]})
    server = ai_client["configure"](sse_script([answer]))
    started = client.post("/api/ai/single/600001").json()
    assert started["accepted"] is True
    state = wait_for(client, "/api/ai/single/600001")
    report = state["report"]
    assert report["ok"] is True and report["chain"] == "single"
    assert [pick["stock_code"] for pick in report["picks"]] == ["600001"]
    assert report["feed_rows"] == 1 and report["board_rows"] >= 1
    assert state["ai_history"]["picks"] == 1 and state["ai_history"]["code"] == "600001"
    body = json.dumps(server.requests[0], ensure_ascii=False)
    assert SECRET not in body and "600001" in body
    # 单股那份提示词只交代这一只：整榜的榜面混进来就是两份不同的输入
    rows = json.loads(server.requests[0]["messages"][1]["content"].split("\n", 1)[1])
    assert [row["stock_code"] for row in rows] == ["600001"]
    assert server.requests[0]["messages"][1]["content"].startswith("这一只股票的证据：")


def test_single_stock_reports_missing_input_without_spending(ai_client):
    """今天没有行情的代码：输入侧就失败，一次网关都不敲。"""
    client = ai_client["client"]
    calls: list[dict] = []

    def script(body, n):
        calls.append(body)
        return sse_script(['{"picks":[]}'])(body, n)

    server = ai_client["configure"](script)
    started = client.post("/api/ai/single/600999")
    assert started.status_code == 200 and started.json()["accepted"] is True, started.text
    state = wait_for(client, "/api/ai/single/600999")
    report = state["report"]
    assert report is not None, state
    assert report["ok"] is False
    assert report["request_sent"] is False
    assert "输入凑不出来" in report["error"]
    assert report["feed_rows"] == 0 and calls == []
    # 没敲网关的失败不进历史：那种「失败」没有成本也没有结论，列表会被开机误点占满
    assert client.get("/api/ai/reports", params={"chain": "single"}).json()["items"] == []


def test_a_finished_run_is_never_readable_as_empty(ai_client, monkeypatch):
    """跑完的那一刻，页面上不许出现「没在跑、也没有结论」那一格。

    注入的违规是可控的一次交错：把后台线程卡在「结论已经算好、还没落地」那一瞬
    （`archive` 之前），轮询那一次读取正好落在这条缝里。旧写法先读结论、后读进度，
    回的就是 `report=None` 配 `running=False`——那份已经跑完的分析只有重进页面才看得见。
    现在两处都改对了：落地与清进度是同一个动作，读取顺序是先问在不在跑、再取结论，
    所以落在缝里的那一次只会说「还在跑」。
    """
    from backend.app.services import ai_analysis_service

    client = ai_client["client"]
    server = ai_client["configure"](sse_script(['{"picks":[]}']))
    original_archive = ai_analysis_service.archive
    original_result = ai_analysis_service.AiAnalysisRunner.result
    landed = threading.Event()
    release = threading.Event()

    def gated_archive(report):
        if not report.get("feed_rows"):
            #: 后台线程停在这里：`_payloads` 还是空的，`_current` 里那一份进度还在
            landed.set()
            release.wait(10)
        return original_archive(report)

    def straddling_result(self, key=""):
        out = original_result(self, key)
        if key == "600999" and not out.get("report"):
            #: 读到空之后才放行——这一次读取就横跨在「落地」两侧
            release.set()
            time.sleep(0.2)
        return out

    monkeypatch.setattr(ai_analysis_service, "archive", gated_archive)
    monkeypatch.setattr(ai_analysis_service.AiAnalysisRunner, "result", straddling_result)
    try:
        assert client.post("/api/ai/single/600999").json()["accepted"] is True
        assert landed.wait(10), "后台线程没停在预期那一瞬，这条判据没被真正走到"
        mid = client.get("/api/ai/single/600999").json()
        assert mid["report"] is not None or mid["runner"]["running"] is True, mid
        state = wait_for(client, "/api/ai/single/600999")
    finally:
        release.set()
    assert state["runner"]["running"] is False
    assert state["report"] is not None, state
    assert state["report"]["ok"] is False and "输入凑不出来" in state["report"]["error"]
    assert server.requests == []


def test_a_crashed_worker_still_leaves_a_readable_report(ai_client, monkeypatch):
    """后台线程整个崩了也要留下一份能读的账：进度清掉而结论没有，等于钱可能花了、页面什么都没有。"""
    from backend.app.services import ai_analysis_service

    client = ai_client["client"]
    ai_client["configure"](sse_script(['{"picks":[]}']))
    runner = ai_analysis_service.single_runner

    def explode(self, key, board_provider, *, selection=None):
        raise ZeroDivisionError("除零：假装分析这一步崩了")

    monkeypatch.setattr(ai_analysis_service.AiAnalysisRunner, "run_once", explode)
    assert client.post("/api/ai/single/600001").json()["accepted"] is True
    state = wait_for(client, "/api/ai/single/600001")
    report = state["report"]
    assert report is not None, state
    assert report["ok"] is False and "后台线程没跑完" in report["error"]
    assert "ZeroDivisionError" in report["error"] and report["request_sent"] is False
    # 槽位必须还回来：否则这只股票以后每次直评都只回「那一份还在跑」，而那份永远不会跑完
    assert runner.status("600001")["running"] is False
    assert client.post("/api/ai/single/600001").json()["accepted"] is True


# --- 每日固定榜 --------------------------------------------------------------


def test_a_ranked_day_is_never_readable_as_still_prepared(ai_client, monkeypatch):
    """排名跑完那一刻，页面不许读到「没在跑、也还没排名」。

    注入的违规和整榜那条同一把尺子：把后台线程钉在「钱正在花」那一瞬（网关先不作声），
    再让读取这一次正好落在跑完之前的库面上——读完了才放行，然后等它把这一趟跑完。
    先读库面、后读进度的写法回的是 `status=prepared` 配 `running=False`：
    那份花钱排好的名次要等用户重进页面才看得见。
    """
    from backend.app.services import ai_daily_rank_service
    from backend.app.services.ai_daily_rank_service import daily_rank_runner

    client = ai_client["client"]
    answer = json.dumps(
        {"criteria": "按量能排", "picks": [{"stock_code": "600000", "score": 90.0, "reason": "封板整齐"}]},
        ensure_ascii=False,
    )
    hold = threading.Event()
    straddled = threading.Event()

    def script(body, n):
        assert hold.wait(10), "假网关没等到放行"
        return sse_script([answer])(body, n)

    server = ai_client["configure"](script)
    original_get_board = ai_daily_rank_service.get_board

    def straddling_get_board(session, day=None):
        row = original_get_board(session, day)
        #: 只有「正在跑排名」那一次轮询要横跨这一瞬：备输入那一步、后台线程自己读的那一行都不算
        if (
            threading.current_thread().name == "AnyIO worker thread"
            and not straddled.is_set()
            and daily_rank_runner.status().get("running")
            and row is not None
            and row.status == "prepared"
        ):
            straddled.set()
            hold.set()  # 后台线程就在这段路里落库、清掉进度
            time.sleep(0.4)
        return row

    monkeypatch.setattr(ai_daily_rank_service, "get_board", straddling_get_board)
    try:
        assert client.post("/api/ai/daily-rank/prepare").json()["ok"] is True
        started = client.post("/api/ai/daily-rank/rank").json()
        assert started["accepted"] is True, started
        deadline = time.monotonic() + 10.0
        mid = None
        while time.monotonic() < deadline:
            payload = client.get("/api/ai/daily-rank").json()
            if straddled.is_set():
                mid = payload
                break
            if not payload["runner"].get("running"):
                break
            time.sleep(0.02)
        assert straddled.is_set(), "轮询没走到那一条缝里，这条判据等于没测"
        board = mid["board"] or {}
        assert board.get("status") == "done" or mid["runner"]["running"] is True, mid
        state = wait_for(client, "/api/ai/daily-rank")
    finally:
        hold.set()
    assert state["runner"]["running"] is False
    assert state["board"]["status"] == "done", state["board"]
    assert state["board"]["result"] is not None
    assert len(server.requests) == 1


def test_prepare_is_free_and_rank_costs(ai_client):
    """两笔账分开：备输入一次网关都不敲，排名那一下才花钱；五道闸各按各自的方式处理。"""
    client = ai_client["client"]
    calls: list[dict] = []

    def script(body, n):
        calls.append(body)
        answer = json.dumps(
            {
                "criteria": "按量能配合排",
                "picks": [
                    {"stock_code": "600000", "score": 91.0, "reason": "封板整齐，量能也跟上", "counter_evidence": "样本只有几天"},
                    {"stock_code": "600001", "score": 80.0, "reason": "编一个追不回的数 12345.6"},
                    {"stock_code": "000002", "reason": "缺分的那一只"},
                    {"stock_code": "600000", "score": 70.0, "reason": "重复"},
                ],
            },
            ensure_ascii=False,
        )
        return sse_script([answer], usage={"total_cost": "0.0258"})(body, n)

    server = ai_client["configure"](script)
    prepared = client.post("/api/ai/daily-rank/prepare").json()
    assert prepared["ok"] is True, prepared
    assert prepared["feed_rows"] == 3  # 榜上就三只
    assert calls == []  # 备输入一分钱没花：一次网关都没敲

    view = client.get("/api/ai/daily-rank").json()["board"]
    assert view["status"] == "prepared"
    assert view["ok"] is False  # 备好输入不等于成功：那一格说的是「还没排名」
    assert view["result"] is None
    assert view["feed_rows"] == 3 and len(view["feed"]) == 3

    started = client.post("/api/ai/daily-rank/rank").json()
    assert started["accepted"] is True
    state = wait_for(client, "/api/ai/daily-rank")
    board = state["board"]
    assert board["status"] == "done" and board["result"] is not None
    # 「成功了吗」得能从库里那份视图直接读出来：后台线程记的那笔账吃的就是这一项
    assert board["ok"] is True and state["runner"]["last"]["ok"] is True
    assert state["runner"]["last"]["request_sent"] is True
    picks = board["result"]["picks"]
    assert [pick["rank"] for pick in picks] == [1, 2]
    assert [pick["stock_code"] for pick in picks] == ["600000", "600001"]
    # 缺分的行剔掉并计数；重复的那条只留第一条；编出来的数只盖章、不删行
    assert board["rejected_count"] == 1 and board["result"]["rejected_codes"] == ["000002"]
    assert board["result"]["duplicate_codes"] == ["600000"]
    assert board["unverified_citations"] == 1
    assert board["result"]["unverified_codes"] == ["600001"]
    assert [pick["unverified_citation"] for pick in picks] == [False, True]
    assert board["result"]["cost_text"] == "成本 0.0258"
    assert board["result"]["formula"].startswith("输入是")
    assert len(calls) == 1


def test_the_cost_line_keeps_the_thinking_tokens_even_on_a_row_cut_by_the_old_column(ai_client):
    """「输入 N tokens · 输出 N tokens · 其中思考 N」轻松过 40 字；老版本把列截在 40 字，
    页面上那格就只剩前两段。现在列放宽，老库里已经被截的那行也从结果里读回全文。"""
    from backend.app.core.database import session_scope
    from backend.app.models.tables import AiDailyRankBoard

    client = ai_client["client"]
    answer = json.dumps({"criteria": "c", "picks": [{"stock_code": "600000", "score": 90.0, "reason": "r"}]})
    usage = {"prompt_tokens": 123456, "completion_tokens": 78901, "completion_tokens_details": {"reasoning_tokens": 65432}}
    ai_client["configure"](sse_script([answer], usage=usage))
    client.post("/api/ai/daily-rank/prepare")
    client.post("/api/ai/daily-rank/rank")
    board = wait_for(client, "/api/ai/daily-rank")["board"]
    full = "输入 123456 tokens · 输出 78901 tokens · 其中思考 65432"
    assert len(full) > 40
    assert board["cost_text"] == full
    assert client.get("/api/ai/daily-rank/history").json()["items"][0]["cost_text"] == full

    with session_scope() as session:
        row = session.query(AiDailyRankBoard).one()
        assert row.cost_text == full
        row.cost_text = full[:40]  # 老版本落下来的那一行
    assert client.get("/api/ai/daily-rank").json()["board"]["cost_text"] == full


def test_re_prepare_pushes_a_ranked_day_back_to_prepared(ai_client):
    """输入换了，旧名次就不是这份输入的名次——留着比空着更坏。"""
    client = ai_client["client"]
    answer = json.dumps({"criteria": "c", "picks": [{"stock_code": "600000", "score": 90.0, "reason": "r"}]})
    server = ai_client["configure"](sse_script([answer]))
    client.post("/api/ai/daily-rank/prepare")
    client.post("/api/ai/daily-rank/rank")
    assert wait_for(client, "/api/ai/daily-rank")["board"]["status"] == "done"
    client.post("/api/ai/daily-rank/prepare")
    board = client.get("/api/ai/daily-rank").json()["board"]
    assert board["status"] == "prepared" and board["result"] is None
    assert board["picks_count"] == 0 and board["cost_text"] == ""
    del server


def test_rank_refuses_without_prepared_input(ai_client):
    client = ai_client["client"]
    server = ai_client["configure"](sse_script(['{"picks":[]}']))
    calls_before = len(server.requests)
    answer = client.post("/api/ai/daily-rank/rank")
    assert answer.status_code == 409
    assert "备好" in answer.json()["detail"]
    assert len(server.requests) == calls_before


def test_prepare_refuses_a_non_trading_day(ai_client, monkeypatch):
    """休市日数据源回的还是上一个交易日的价：不能把那一天的输入和名次覆盖掉。

    这里不动库里的行情，只把时钟往前挪一天——正是「今天不是交易日」的现实形状，
    也不会因为改行情撞出唯一键冲突。
    """
    client = ai_client["client"]
    from backend.app.core import clock

    monkeypatch.setattr(clock, "today", lambda: ai_client["last_day"] + timedelta(days=1))
    server = ai_client["configure"](sse_script(['{"picks":[]}']))
    result = client.post("/api/ai/daily-rank/prepare").json()
    assert result["ok"] is False and "今天不是交易日" in result["error"]
    assert result["feed_rows"] == 0
    assert len(server.requests) == 0
    assert client.get("/api/ai/daily-rank").json()["board"] is None


def test_daily_rank_history_lists_days(ai_client):
    client = ai_client["client"]
    answer = json.dumps({"criteria": "c", "picks": [{"stock_code": "600000", "score": 90, "reason": "r"}]})
    server = ai_client["configure"](sse_script([answer]))
    client.post("/api/ai/daily-rank/prepare")
    client.post("/api/ai/daily-rank/rank")
    wait_for(client, "/api/ai/daily-rank")
    history = client.get("/api/ai/daily-rank/history").json()
    assert history["items"][0]["status"] == "done"
    assert history["items"][0]["trade_date"] == ai_client["last_day"].isoformat()
    assert history["feed_limit"] == 100 and history["pick_count"] == 20
    del server


# --- 提示词的口径 ------------------------------------------------------------


def test_prompt_carries_the_formula_and_null_rule(ai_client):
    client = ai_client["client"]
    server = ai_client["configure"](sse_script(['{"picks":[]}']))
    client.post("/api/ai/analysis")
    wait_for(client, "/api/ai/analysis")
    messages = server.requests[0]["messages"]
    system = messages[0]["content"]
    assert "综合评分按固定权重计算" in system
    assert "null 不是 0" in system
    assert "凭空的事实不能用" in system
    user = messages[1]["content"]
    assert user.startswith("盘中监控池（本系统监控榜当前排序的前 3 行）：")
    rows = json.loads(user.split("\n", 1)[1])
    assert all("formula" in item for item in rows)
    assert all(item["max_similarity"] is not None for item in rows)
    assert all(item["feed_rank"] == index + 1 for index, item in enumerate(rows))


def test_daily_rank_prompt_says_who_owns_the_rank(ai_client):
    """固定榜特有的那一句：名次归模型的分，综合评分归本系统，别让它照着抄。"""
    client = ai_client["client"]
    server = ai_client["configure"](sse_script(['{"picks":[]}']))
    client.post("/api/ai/daily-rank/prepare")
    client.post("/api/ai/daily-rank/rank")
    wait_for(client, "/api/ai/daily-rank")
    system = server.requests[0]["messages"][0]["content"]
    assert "数组第一个就是你认为最好的那只" in system
    assert "输入是 " + ai_client["last_day"].isoformat() in system


def test_the_scoring_wording_follows_the_compare_window_setting(ai_client):
    """对比窗口在设置里改得动：口径句、指标中文名、提示词里那个天数必须跟着改。

    「近五日」「近三日」都曾写死在源码里——窗口一改成 5，页面上说的天数和
    样本池数出来的天数就不是同一件事，模型也照着旧天数读证据。"""
    from backend.app.services.ai_evidence import indicator_labels
    from backend.app.services.score_model import formula_text

    client = ai_client["client"]
    assert client.put("/api/settings", json={"compare_window_trading_days": 3}).status_code == 200
    assert "那 3 个交易日" in formula_text()
    assert indicator_labels()["pattern_match_rate"] == "近 3 日高相似占比"

    changed = client.put("/api/settings", json={"compare_window_trading_days": 5})
    assert changed.status_code == 200, changed.text
    _wait_window_job(client)
    assert "那 5 个交易日" in formula_text(), "口径句还挂着旧窗口"
    assert indicator_labels()["pattern_match_rate"] == "近 5 日高相似占比"

    # 固定榜页面上那句口径是 GET 现读的：那里没有别的渠道能看到窗口改了没。
    prepared = client.post("/api/ai/daily-rank/prepare")
    assert prepared.status_code == 200, prepared.text
    daily_board = client.get("/api/ai/daily-rank").json()["board"]
    assert daily_board is not None
    assert "那 5 个交易日" in daily_board["formula"], daily_board["formula"]

    server = ai_client["configure"](sse_script(['{"picks":[]}']))
    client.post("/api/ai/analysis")
    report = wait_for(client, "/api/ai/analysis")["report"]
    assert "那 5 个交易日" in report["formula"], report["formula"]
    assert "那 5 个交易日" in server.requests[0]["messages"][0]["content"]
    # 形态窗口是固定的 3 根，不跟对比窗口跑：那句「3日形态相似度」不能一起变。
    assert indicator_labels()["similarity_score"] == "3日形态相似度"


def test_the_three_day_features_come_from_the_evaluation_day(ai_client):
    """技术/量能/风险那三项必须吃评估日那一根：吃基准日就是拿昨天的涨幅评今天的板。

    判据两头都要落：正常回放取 `trade_date`；那一根不在库里（盘中还没落库）
    才退回基准日，并且把「退到了」这件事写进回包。"""
    from backend.app.core.database import session_scope
    from backend.app.services import ai_evidence, realtime_service
    from backend.app.services import score_model
    from backend.app.services.ai_evidence import build_evidence, features_at

    client = ai_client["client"]
    with session_scope() as session:
        result = realtime_service.scan(session, mode=realtime_service.MODE_SETTLED)
        board = {key: value for key, value in result.items() if key != "rows"}
        board["limit_up_rows"] = [row for row in result["rows"] if row["is_limit_up"]]
        board["pending_rows"] = [row for row in result["rows"] if not row["is_limit_up"]]
        assert board["pending_rows"] or board["limit_up_rows"], "这份榜一行都没有"
        bundle = build_evidence(session, board)
    assert bundle["rows"], "这份榜一行证据都没有"
    assert bundle["indicator_date"] == board["trade_date"]
    assert bundle["indicator_date_source"] == "trade_date"

    code = bundle["rows"][0]["stock_code"]
    with session_scope() as session:
        on_day = features_at(session, [code], date.fromisoformat(board["trade_date"]))
        previous = features_at(session, [code], date.fromisoformat(board["base_date"]))
    assert code in on_day and code in previous
    assert on_day[code] != previous[code], "两根线的特征一模一样，这条判据没有区分力"
    #: 这三项各自从哪一根线算出来，榜上的分必须等于「评估日那一根」的那一份，
    #: 并且至少有一项与「基准日那一根」算出来的不同——不然上面那串等号是恒等式，不是判据。
    from_day = {
        "technical_score": score_model.technical_from_features(on_day[code]),
        "volume_score": score_model.volume_from_features(on_day[code]),
        "risk_score": score_model.risk_from_features(on_day[code]),
    }
    from_previous = {
        "technical_score": score_model.technical_from_features(previous[code]),
        "volume_score": score_model.volume_from_features(previous[code]),
        "risk_score": score_model.risk_from_features(previous[code]),
    }
    row_scores = bundle["rows"][0]["indicator_scores"]
    for key, value in from_day.items():
        assert row_scores[key] == value, (key, row_scores[key], value)
    moved = [key for key in from_day if from_day[key] != from_previous[key]]
    assert moved, f"两根线的分完全一样：{from_day}"

    shifted = dict(board)
    shifted["trade_date"] = (date.fromisoformat(board["trade_date"]) + timedelta(days=1)).isoformat()
    with session_scope() as session:
        retreated = build_evidence(session, shifted)
    assert retreated["rows"], "换了评估日之后证据行没了，那条断言就成了空话"
    assert retreated["indicator_date"] == board["base_date"]
    assert retreated["indicator_date_source"] == "fallback_base_date"
    assert client.get("/api/health").status_code == 200
