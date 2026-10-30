"""流式：慢思考不超时、空闲超时按块计算、总时长上限、排名批次超时拆半。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.app.core import ai_client
from backend.app.core.ai_client import AiFailed, AiTimeout


def _chunk(delta: dict) -> bytes:
    return f"data: {json.dumps({'choices': [{'delta': delta}]}, ensure_ascii=False)}\n\n".encode()


def _server(script):
    """script(handler) 负责写出 SSE；返回 (base_url, server)。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            assert body.get("stream") is True
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("connection", "close")
            self.end_headers()
            try:
                script(self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/v1", server


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(ai_client, "_TRANSPORT", None)
    monkeypatch.setattr(ai_client, "system_proxy", lambda url: None)
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    servers = []

    def start(script):
        url, server = _server(script)
        servers.append(server)
        return {"ai_base_url": url, "ai_model": "qwen3.8", "ai_api_key": "sk-x", "ai_thinking": "on"}

    yield start
    for server in servers:
        server.shutdown()


def test_slow_thinking_stream_survives_longer_than_idle_timeout(local):
    def script(out):
        for _ in range(8):  # 共约 2.4 秒的思考，远超 1 秒空闲超时
            out.write(_chunk({"reasoning_content": "思考中"}))
            out.flush()
            time.sleep(0.3)
        out.write(_chunk({"content": "好了"}) + b"data: [DONE]\n\n")
        out.flush()

    settings = local(script)
    started = time.monotonic()
    assert ai_client.chat("s", "p", settings=settings, read_timeout=1) == "好了"
    assert time.monotonic() - started > 2.0


def test_idle_timeout_is_per_chunk(local):
    def script(out):
        out.write(_chunk({"reasoning_content": "开始"}))
        out.flush()
        time.sleep(2.5)  # 之后一直没动静

    settings = local(script)
    with pytest.raises(AiTimeout) as info:
        ai_client.chat("s", "p", settings=settings, read_timeout=1)
    assert "连续 1 秒没有收到任何数据" in str(info.value)


def test_total_cap_stops_endless_thinking(local):
    def script(out):
        for _ in range(20):
            out.write(_chunk({"reasoning_content": "还在想"}))
            out.flush()
            time.sleep(0.2)

    settings = local(script)
    with pytest.raises(AiTimeout) as info:
        ai_client.chat("s", "p", settings=settings, read_timeout=1, total_timeout=1.0)
    assert "总时长超时" in str(info.value)


def test_aliyun_always_streams_even_in_auto(monkeypatch):
    import httpx

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=_chunk({"content": "ok"}).decode(), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(ai_client, "_TRANSPORT", httpx.MockTransport(handler))
    settings = {"ai_base_url": "https://ws-x.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "ai_model": "qwen", "ai_api_key": "sk-x"}
    assert ai_client.chat("s", "p", settings=settings) == "ok"
    assert seen["body"]["stream"] is True


def test_batch_timeout_splits_in_half_and_keeps_partial(monkeypatch):
    from backend.app.core import config
    from backend.app.services import ranking_service

    monkeypatch.setattr(config, "ai_int", lambda raw, name: {"ai_batch_size": 10, "ai_parallel": 3}.get(name, 120))
    monkeypatch.setattr(ranking_service, "_prompt", lambda day, items, market="": "\n".join(f"{i['code']} x" for i in items))
    items = [{"code": f"{600000 + i}"} for i in range(25)]  # 默认每批 10 只 → 3 批

    def chat(prompt):
        codes = [line.split()[0] for line in prompt.splitlines()]
        if len(codes) > 5:
            raise AiTimeout("读取超时")
        if "600024" in codes:
            raise AiFailed("这一小批坏了")
        return json.dumps({"items": [{"code": c, "score": 50} for c in codes]})

    from datetime import date

    scores = ranking_service._score_batches(date(2026, 10, 30), items, chat)
    progress = ranking_service.progress()
    assert progress["total"] == 3 and progress["batch_size"] == 10
    assert progress["split"] == 2  # 两个 10 只的批拆半；最后 5 只那批没超时
    assert progress["failed"] == 1 and progress["done"] == 3
    assert len(scores) == 20 and "600024" not in scores and "600000" in scores


def test_ai_settings_validated(client):
    saved = client.put("/api/settings", json={"ai_timeout_seconds": 200, "ai_batch_size": 6, "ai_parallel": 2}).json()
    assert (saved["ai_timeout_seconds"], saved["ai_batch_size"], saved["ai_parallel"]) == (200, 6, 2)
    assert client.put("/api/settings", json={"ai_timeout_seconds": 5}).status_code == 422
    assert client.put("/api/settings", json={"ai_batch_size": 0}).status_code == 422
