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


class _Client:
    def __init__(self, behaviour, captured):
        self.behaviour, self.captured = behaviour, captured

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self.captured.update(url=url, json=json)
        return self.behaviour(url)


def _patch(monkeypatch, behaviour, captured=None):
    captured = {} if captured is None else captured
    monkeypatch.setattr(ai_client.httpx, "Client", lambda **kwargs: _Client(behaviour, captured))
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
    assert captured["json"]["enable_thinking"] is False


def test_ai_test_endpoint(client, monkeypatch):
    def ok(url):
        return httpx.Response(200, json={"choices": [{"message": {"content": "连接成功"}}]}, request=httpx.Request("POST", url))

    _patch(monkeypatch, ok)
    result = client.post("/api/ai/test", json=SETTINGS).json()
    assert result["ok"] is True and result["reply"] == "连接成功"
    assert "latency_ms" in result and KEY not in str(result)
    assert client.post("/api/ai/test", json={}).json()["ok"] is False
