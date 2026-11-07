"""AI 接口：地址补全、具体错误、密钥不外泄、测试接口。"""

import httpx
import pytest

from backend.app.core import ai_client
from backend.app.core.ai_client import AiFailed, normalize_endpoint

KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"
SETTINGS = {"ai_base_url": "https://ws-x.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "ai_model": "qwen", "ai_api_key": KEY}


def test_normalize_endpoint():
    assert normalize_endpoint("https://ws-x.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")[0] == (
        "https://ws-x.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    )
    url, note = normalize_endpoint("https://ws-x.cn-beijing.maas.aliyuncs.com")
    assert url.endswith("/compatible-mode/v1/chat/completions") and "已自动补上" in note
    assert normalize_endpoint("api.deepseek.com/v1")[0] == "https://api.deepseek.com/v1/chat/completions"
    assert normalize_endpoint("https://x/v1/chat/completions/")[0] == "https://x/v1/chat/completions"


def _patch(monkeypatch, behaviour, captured=None):
    captured = {} if captured is None else captured
    calls = captured.setdefault("calls", [])

    def handler(request):
        import json as _json

        body = _json.loads(request.content or b"{}")
        captured.update(url=str(request.url), json=body)
        calls.append(body)
        return behaviour(str(request.url)) if behaviour.__code__.co_argcount == 1 else behaviour(str(request.url), body)

    monkeypatch.setattr(ai_client, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(ai_client, "_save_learned", lambda *a: captured.setdefault("learned", a))
    return captured


@pytest.mark.parametrize(
    "exc, words",
    [
        (httpx.ReadTimeout("timed out"), "读取超时"),
        (httpx.ConnectTimeout("timed out"), "连接超时"),
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "域名解析失败"),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "SSL"),
        (httpx.ConnectError("[WinError 10061] refused"), "连接被拒绝"),
        (httpx.ProxyError("bad proxy"), "代理"),
    ],
)
def test_network_errors_are_specific(monkeypatch, exc, words):
    def boom(url):
        raise exc

    _patch(monkeypatch, boom)
    with pytest.raises(AiFailed) as info:
        ai_client.chat("s", "p", settings=SETTINGS, read_timeout=5)
    assert words in str(info.value)
    assert "compatible-mode/v1/chat/completions" in str(info.value)
    assert KEY not in str(info.value)


def test_http_error_shows_status_and_body_without_key(monkeypatch):
    def unauthorized(url):
        return httpx.Response(401, text=f'{{"error":"Incorrect API key provided: {KEY}"}}', request=httpx.Request("POST", url))

    captured = _patch(monkeypatch, unauthorized)
    with pytest.raises(AiFailed) as info:
        ai_client.chat("s", "p", settings=SETTINGS)
    text = str(info.value)
    assert "HTTP 401" in text and "Incorrect API key" in text and KEY not in text
    assert "enable_thinking" not in captured["json"]  # 自动模式不发送该参数


THINK_400 = '{"error":{"code":"invalid_parameter_error","message":"The value of the enable_thinking parameter is restricted to True."}}'


def _sse(*chunks):
    import json as _json

    lines = [f"data: {_json.dumps(c, ensure_ascii=False)}" for c in chunks] + ["data: [DONE]"]
    return "\n\n".join(lines) + "\n\n"


def test_retry_with_thinking_and_stream(monkeypatch):
    def behaviour(url, body):
        if body.get("enable_thinking") is not True:
            return httpx.Response(400, text=THINK_400)
        assert body.get("stream") is True
        text = _sse(
            {"choices": [{"delta": {"reasoning_content": "先想一想"}}]},
            {"choices": [{"delta": {"content": "连接"}}]},
            {"choices": [{"delta": {"content": "成功"}}]},
        )
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    captured = _patch(monkeypatch, behaviour)
    assert ai_client.chat("s", "p", settings=SETTINGS) == "连接成功"
    assert len(captured["calls"]) == 2
    assert captured["learned"][2:] == (True, True)


def test_retry_opposite_when_off_rejected(monkeypatch):
    def behaviour(url, body):
        if body.get("enable_thinking") is True:
            return httpx.Response(400, text='{"error":{"message":"enable_thinking must be false for this model"}}')
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok", "reasoning_content": "x"}}]})

    captured = _patch(monkeypatch, behaviour)
    settings = {**SETTINGS, "ai_thinking_learned": {f"{SETTINGS['ai_base_url']}|qwen": {"thinking": True, "stream": False}}}
    captured["calls"].clear()
    assert ai_client.chat("s", "p", settings=settings) == "ok"
    assert [c.get("enable_thinking") for c in captured["calls"]] == [True, False]


def test_forced_mode_sent(monkeypatch):
    captured = _patch(monkeypatch, lambda url: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    ai_client.chat("s", "p", settings={**SETTINGS, "ai_thinking": "off"})
    assert captured["json"]["enable_thinking"] is False


def test_parse_stream_skips_reasoning():
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"思考"}}]}',
        "",
        ": keep-alive",
        'data: {"choices":[{"delta":{"content":"{\\"a\\":1}"}}]}',
        "data: not-json",
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"after"}}]}',
    ]
    assert ai_client.parse_stream(lines) == '{"a":1}'


def test_ai_test_endpoint(client, monkeypatch):
    def ok(url):
        return httpx.Response(200, json={"choices": [{"message": {"content": "连接成功"}}]}, request=httpx.Request("POST", url))

    _patch(monkeypatch, ok)
    result = client.post("/api/ai/test", json=SETTINGS).json()
    assert result["ok"] is True and result["reply"] == "连接成功"
    assert "latency_ms" in result and KEY not in str(result)
    assert client.post("/api/ai/test", json={}).json()["ok"] is False
