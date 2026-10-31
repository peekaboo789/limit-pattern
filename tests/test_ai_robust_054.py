"""v0.5.4：AI 打分稳健性——百炼 qwen 默认关思考、max_tokens 兜底、429/5xx 退避重试、截断/围栏/思考段的 JSON 容错、
失败按原始批次计数、原因分类、测试连接带一次真实打分。"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from backend.app.core import ai_client
from backend.app.core.ai_client import AiFailed
from backend.app.services import ranking_service
from backend.app.services.summary_service import salvage_objects

KEY = "sk-test-fake-robust-0001"
DASH = {"ai_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "ai_model": "qwen3.5-plus", "ai_api_key": KEY, "ai_max_tokens": 1024}


def _items(codes, reason="形态接近"):
    return {"items": [{"code": c, "score": 60, "reason": reason} for c in codes]}


def _sse(content: str, finish="stop", reasoning: str = "") -> bytes:
    out = b""
    if reasoning:
        out += f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': reasoning}}]}, ensure_ascii=False)}\n\n".encode()
    out += f"data: {json.dumps({'choices': [{'delta': {'content': content}, 'finish_reason': finish}]}, ensure_ascii=False)}\n\n".encode()
    return out + b"data: [DONE]\n\n"


@pytest.fixture()
def transport(monkeypatch):
    calls: list[dict] = []
    box = {"handler": None}

    def handle(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        assert KEY not in json.dumps(body)
        return box["handler"](body, len(calls))

    monkeypatch.setattr(ai_client, "_TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(ai_client, "_sleep", lambda s: None)
    monkeypatch.setattr(ai_client, "_save_learned", lambda *a: None)
    return box, calls


def _codes(body):
    return [ln.split()[0] for ln in body["messages"][1]["content"].splitlines() if ln[:1].isdigit() and ln.split()[0].isdigit()]


def test_dashscope_scoring_turns_thinking_off_and_raises_max_tokens(transport):
    box, calls = transport
    fenced = "```json\n" + json.dumps(_items(["600000"])) + "\n```"
    box["handler"] = lambda body, n: (httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(fenced, reasoning="想一想"))
                                      if body.get("stream") else httpx.Response(200, json={"choices": [{"message": {"content": fenced}, "finish_reason": "stop"}]}))
    text = ai_client.chat("s", "p", settings=DASH, thinking_default=False, min_tokens=2600)
    assert calls[-1]["enable_thinking"] is False and calls[-1]["max_tokens"] == 2600 and calls[-1]["stream"] is True
    assert "600000" in text and "想一想" not in text
    # 用户显式开思考时尊重设置
    ai_client.chat("s", "p", settings=dict(DASH, ai_thinking="on"), thinking_default=False)
    assert calls[-1]["enable_thinking"] is True
    # 非百炼（如 DeepSeek）不乱加参数
    ai_client.chat("s", "p", settings=dict(DASH, ai_base_url="https://api.deepseek.com/v1", ai_model="deepseek-chat"), thinking_default=False)
    assert "enable_thinking" not in calls[-1]
    assert ai_client.provider("https://api.deepseek.com/v1/chat/completions") == "deepseek"


def test_429_and_5xx_retry_then_succeed(transport):
    box, calls = transport
    seq = [httpx.Response(429, headers={"retry-after": "1"}, text="rate limited"), httpx.Response(503, text="busy"),
           httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})]
    box["handler"] = lambda body, n: seq[n - 1]
    settings = dict(DASH, ai_base_url="https://api.deepseek.com/v1")
    assert ai_client.chat("s", "p", settings=settings) == "ok" and len(calls) == 3


def test_429_exhausted_reports_kind_without_key(transport):
    box, _ = transport
    box["handler"] = lambda body, n: httpx.Response(429, text=f"too many {KEY}")
    with pytest.raises(AiFailed) as err:
        ai_client.chat("s", "p", settings=dict(DASH, ai_base_url="https://api.deepseek.com/v1"))
    assert err.value.kind == "429 限流" and KEY not in str(err.value)
    box["handler"] = lambda body, n: httpx.Response(401, text="bad key")
    with pytest.raises(AiFailed) as err:
        ai_client.chat("s", "p", settings=dict(DASH, ai_base_url="https://api.deepseek.com/v1"))
    assert err.value.kind == "HTTP 401"


def test_truncated_json_is_salvaged():
    full = json.dumps(_items(["600000", "600001", "600002"]), ensure_ascii=False)
    cut = full[: full.index("600002") + 10]
    got = ranking_service._parse_scores(cut, partial=True)
    assert set(got) == {"600000", "600001"}
    assert len(salvage_objects("<think>{\"code\": 1</think>前言 " + cut)) == 2
    with pytest.raises(AiFailed) as err:
        ranking_service._parse_scores("完全不是 JSON")
    assert err.value.kind == "JSON 解析失败"


def _setup(monkeypatch, size=10, parallel=3):
    from backend.app.core import config

    monkeypatch.setattr(config, "ai_int", lambda raw, name: {"ai_batch_size": size, "ai_parallel": parallel}.get(name, 120))
    monkeypatch.setattr(ranking_service, "_prompt", lambda day, items, market="": "\n".join(f"{i['code']} x" for i in items))
    return [{"code": f"{600000 + i}"} for i in range(100)]


def test_end_to_end_qwen_like_provider(monkeypatch, transport):
    """模拟用户现场：qwen 默认思考、max_tokens 1024 只够 4 只、偶发 429。修复后 100 只全部拿到。"""
    box, calls = transport
    items = _setup(monkeypatch)

    def handler(body, n):
        if n % 4 == 0:
            return httpx.Response(429, text="Throttling")
        codes = _codes(body)
        fit = max(1, (body.get("max_tokens") or 4096) // 220)
        finish = "length" if len(codes) > fit else "stop"
        text = json.dumps(_items(codes[:fit]), ensure_ascii=False)
        if finish == "length":
            text = text[:-5]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(text, finish))

    box["handler"] = handler
    scores = ranking_service._score_batches(date(2026, 10, 30), items, None, settings=DASH)
    p = ranking_service.progress()
    assert len(scores) == 100 and p["failed"] == 0 and p["done"] == p["total"] == 10
    assert all(c.get("enable_thinking") is False and c["max_tokens"] >= 220 * len(_codes(c)) for c in calls)


def test_failure_counts_never_exceed_batches(monkeypatch, transport):
    box, _ = transport
    items = _setup(monkeypatch)
    box["handler"] = lambda body, n: httpx.Response(200, json={"choices": [{"message": {"content": "抱歉我无法回答"}, "finish_reason": "stop"}]})
    with pytest.raises(AiFailed) as err:
        ranking_service._score_batches(date(2026, 10, 30), items, None, settings=dict(DASH, ai_base_url="https://api.deepseek.com/v1"))
    p = ranking_service.progress()
    assert p["failed"] == 10 and p["total"] == 10 and "JSON 解析失败" in str(err.value) and p["errors"]["JSON 解析失败"] >= 10


def test_ranking_model_label_fixed_at_start(monkeypatch):
    """开始时的模型就是标签和实际调用的模型（中途切换不串）。"""
    seen = []
    monkeypatch.setattr(ranking_service, "call_chat", lambda prompt, **kw: seen.append(kw["settings"]["ai_model"]) or json.dumps(_items([ln.split()[0] for ln in prompt.splitlines()])))
    items = _setup(monkeypatch, size=50, parallel=1)
    settings = dict(DASH)
    ranking_service._score_batches(date(2026, 10, 30), items, None, settings=settings)
    assert set(seen) == {"qwen3.5-plus"} and ranking_service.progress()["model"] == "qwen3.5-plus"


def test_connection_test_runs_scoring_probe(transport):
    box, calls = transport

    def handler(body, n):
        if "连接测试" in body["messages"][1]["content"]:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(_items(["600000", "000001"]))}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "连接成功"}, "finish_reason": "stop"}]})

    box["handler"] = handler
    r = ai_client.test_ai(dict(DASH, ai_base_url="https://api.deepseek.com/v1"))
    assert r["ok"] and r["scoring_ok"] and r["scoring_count"] == 2 and r["provider"] == "deepseek" and KEY not in json.dumps(r)
    box["handler"] = lambda body, n: httpx.Response(200, json={"choices": [{"message": {"content": "连接成功" if n == 1 else "不给 JSON"}, "finish_reason": "stop"}]})
    calls.clear()
    r = ai_client.test_ai(dict(DASH, ai_base_url="https://api.deepseek.com/v1"))
    assert r["ok"] and r["scoring_ok"] is False and "JSON" in r["scoring_error"]
