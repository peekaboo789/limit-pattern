"""大模型网关。三条付费链路（盘中整榜、每日固定榜、单股直评）都走这一条路。

用哪一家由 `core/ai_sources.py` 那张清单决定：设置页那三格是清单第一条，页面上自建的那几条
是后面的，`active_id` 指到哪一条，这一层就敲哪一条。业务代码只问 `ai_config()` 要一套
自洽的「地址 + 模型名 + 密钥」，不在这里再判一次「谁在用」。
这里刻意不发 `max_tokens`：老工程实测推理型模型会把输出预算全花在思考上，
带上 800 的回车只给回 200 字节的空正文，那种失败在页面上说不清楚。

流式是唯一的使用方式：整榜要跑一分多钟，非流式要么把读超时抬到没人敢信的数字，
要么在半路被判死。流式还能把「已经收到多少字」交给页面——钱花到哪儿了，屏幕上得看得见。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from backend.app.core import ai_sources
from backend.app.core.config import scrub_secrets
from backend.app.core.constants import (
    AI_PROGRESS_STEP_CHARS,
    AI_RETRY_WAIT_LIMIT_SECONDS,
    AI_RUN_TIMEOUT_SECONDS,
    AI_STREAM_CONNECT_SECONDS,
    AI_STREAM_STALL_SECONDS,
    AI_TRANSIENT_RETRY_ATTEMPTS,
    AI_TRANSIENT_RETRY_BACKOFF,
)
from backend.app.core.logging import get_logger

logger = get_logger("ai_gateway")


class AiNotConfigured(RuntimeError):
    """当前那一条网关的三件套没配齐。这种失败一分钱都不会花，页面上要这么写。"""


#: 三件套在页面上的名字。缺哪一样就说哪一样，别让人去猜是地址还是钥匙。
_CONFIG_LABELS = {"base_url": "网关地址", "model": "模型名", "api_key": "密钥"}


def not_configured_message() -> str:
    """「没配齐」那句唯一的一句话：当前是哪一条网关、缺的哪几样、这一趟花没花钱。

    为什么不是一句固定的「三项要在设置页填齐」：清单里那十几条自建网关各有各的三件套，
    用户可能正指着清单第三条——告诉他「去填设置页」会把他支到另一条网关上去，
    补齐了照样 401。四条链路（整榜、直评、固定榜、每日总结）以前各写一份这句话，
    现在都从这里读。
    """
    config = ai_config()
    missing = [_CONFIG_LABELS[key] for key in _CONFIG_LABELS if not config.get(key)]
    return (
        f"当前生效的网关是「{config['source_name'] or '没起名的那一条'}」，"
        f"缺的是{'、'.join(missing)}——补齐才敲得出去，这一次一分钱都没花"
    )


def not_configured() -> AiNotConfigured:
    return AiNotConfigured(not_configured_message())


class AiGatewayError(RuntimeError):
    """请求已经交给网关（或者根本没交出去），总之后来没有可用的回答。

    `sent` 说的是「请求有没有真的发出去」：连不上是 False，发出去没回话是 True。
    `billed` 说的是「这一趟按 token 计了费没有」：上游在做出结论之前就把这趟挡回来
    （429 并发满、401/403 密钥、404 模型名不对、5xx 过载）是 False；只要 HTTP 200
    开始吐字了，哪怕半路断流也只能算 True——那时候钱可能已经花出去了。
    页面上「钱花没花」那一句全靠这两个布尔，不能含糊。

    `transient` 是「等一会儿重发有可能成」，`retry_after` 是建议等待的秒数。
    """

    def __init__(self, message: str, *, sent: bool = True, billed: bool = True,
                 transient: bool = False, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.sent = sent
        self.billed = billed
        self.transient = transient
        self.retry_after = retry_after
        self.rejects_response_format = mentions_response_format(message)


def carry(message: str, error: "AiGatewayError") -> "AiGatewayError":
    """换一句话重抛同一个错：`sent/billed/transient/retry_after` 必须跟着走。

    丢掉它们就等于把「这趟没花钱」这条结论在下一层改口成「花了钱」。
    """
    return AiGatewayError(
        message,
        sent=error.sent,
        billed=error.billed,
        transient=error.transient,
        retry_after=error.retry_after,
    )


def ai_config() -> dict[str, str]:
    """当前生效那一条网关的三件套。

    读的是 `ai_sources.resolve_config()`——设置页那三格与清单里自建的那十几条都从这
    一只手出去，所以三条付费链路（整榜、单股直评、固定榜排名）跟着页面上「当前」那一条走，
    这里不存在第二份「谁在用」的判断。
    """
    config = ai_sources.resolve_config()
    return {
        "base_url": config["base_url"].rstrip("/"),
        "model": config["model"],
        "api_key": config["api_key"],
        "source_id": config["source_id"],
        "source_name": config["source_name"],
    }


def ai_ready() -> bool:
    config = ai_config()
    return bool(config["base_url"] and config["model"] and config["api_key"])


def model_name() -> str:
    return ai_config()["model"]


def chat_endpoint(base_url: str) -> str:
    return base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"


def mentions_response_format(text: str) -> bool:
    """上游是不是在说「我不认 response_format 这个参数」。是就值得去掉它重发一次。"""
    lowered = (text or "").lower()
    return "response_format" in lowered or "json_object" in lowered


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _scrub(text: object) -> str:
    """把这一趟可能撞见凭据的文案抹干净。

    名单里要给全：清单上那十几条网关各有一把钥匙，报错文案出自哪一条只有调用方清楚，
    而「上游把请求 URL 原样回显」这种事不会挑那一条正在用的。收在这一个函数里，
    三个调用点（HTTP 错误体、流式中途报错、重发日志）就不会各写一份名单。
    """
    return scrub_secrets(str(text or ""), keys=ai_sources.known_keys())


def _usage_of(payload: dict) -> dict[str, Any]:
    """把上游的 usage 归成一个固定形状。上游没给的项留 null，不补 0。"""
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    cost = usage.get("cost")
    if cost is None:
        cost = usage.get("total_cost")
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "cost": None if cost is None else str(cost),
        "finish_reason": None,
    }


def _failure(status: int, snippet: str) -> str:
    text = _scrub(snippet[:400]).strip()
    return f"上游 HTTP {status}" + (f"：{text}" if text else "")


#: 这些状态码的意思是「服务端现在忙/不接这单」，等一会儿再敲有意义。
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504, 509})
#: 上游不一定给 429，它也可能用 400 回一句「并发满了，请稍后再试」。
TRANSIENT_MARKERS = (
    "concurrency",
    "rate_limit",
    "rate limit",
    "too many requests",
    "retry later",
    "please retry",
    "overloaded",
    "限流",
    "并发",
    "请稍后",
    "稍后再试",
)


def _looks_transient(status: int, text: str) -> bool:
    """这趟是不是「还没开始做结论就被挡回来、等一下重发可能成」。

    只认上游明说的：429/5xx，或者正文里带限流、并发、稍后再试那几类字。
    参数报错（`response_format` 不认）不算——那种要靠去掉参数重发，多敲几次只是多花一趟。
    """
    lowered = (text or "").lower()
    if mentions_response_format(lowered):
        return False
    if status in TRANSIENT_STATUS:
        return True
    return status >= 400 and any(marker in lowered for marker in TRANSIENT_MARKERS)


def _retry_wait(headers, text: str, attempt: int) -> float:
    """这一趟之后等多久。上游给了 `Retry-After` 就听它的，否则按退避表。"""
    raw = ""
    try:
        raw = str(headers.get("Retry-After") or "").strip()
    except Exception:  # noqa: BLE001 - 头没了就退回退避表，不值得为此炸掉这一趟
        raw = ""
    if raw:
        try:
            seconds = float(raw)
        except ValueError:
            seconds = -1.0
        if seconds >= 0:
            return min(seconds, AI_RETRY_WAIT_LIMIT_SECONDS)
    index = max(1, attempt) - 1
    table = AI_TRANSIENT_RETRY_BACKOFF
    base = table[index] if index < len(table) else table[-1]
    return min(base, AI_RETRY_WAIT_LIMIT_SECONDS)


def _stream_attempt(config: dict[str, str], body: dict, timeout: httpx.Timeout,
                    deadline: float, timeout_seconds: float,
                    on_progress: Callable[[int, int], None] | None,
                    attempt: int = 1) -> tuple[str, dict]:
    """敲一次网关、把流读完。失败一律抛 `AiGatewayError`，带清 `sent/billed/transient`。"""
    pieces: list[str] = []
    answer_chars = 0
    thinking_chars = 0
    usage: dict[str, Any] = _usage_of({})
    last_reported = 0
    sent = False
    try:
        with httpx.Client(timeout=timeout, headers=_headers(config["api_key"])) as client:
            with client.stream("POST", chat_endpoint(config["base_url"]), json=body) as response:
                sent = True
                if response.status_code >= 400:
                    snippet = ""
                    try:
                        #: `read()` 没有 `max_chunk_size` 这个参数（那是 requests 的写法）：
                        #: 带上它就每次都抛 TypeError、被下面那个 except 吞掉，
                        #: 于是上游明明回了「额度用尽」「密钥无效」，页面上只剩一句「HTTP 403」。
                        snippet = response.read()[:4096].decode("utf-8", "replace")
                    except Exception:  # noqa: BLE001 - 读错误体失败不该盖住真正的状态码
                        pass
                    raise AiGatewayError(
                        _failure(response.status_code, snippet),
                        sent=True,
                        #: 一个 token 都还没生成，这一趟按 token 计不了费
                        billed=False,
                        transient=_looks_transient(response.status_code, snippet),
                        retry_after=_retry_wait(response.headers, snippet, attempt),
                    )
                for line in response.iter_lines():
                    if time.monotonic() > deadline:
                        raise AiGatewayError(
                            f"整趟超过 {timeout_seconds:.0f} 秒上限，中途放弃（已收到 {answer_chars} 字）",
                            sent=True,
                        )
                    text = (line or "").strip()
                    if not text.startswith("data:"):
                        continue
                    payload = text[len("data:"):].strip()
                    if payload in {"", "[DONE]"}:
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    _merge_usage(usage, chunk)
                    error = chunk.get("error")
                    if error:
                        raise AiGatewayError(
                            _scrub(f"流式中途报错：{json.dumps(error, ensure_ascii=False)}")[:1000]
                        )
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        #: 思考字数单独数。推理模型把预算全花在思考上时，正文一帧都不发，
                        #: 而「思考了 4096 字然后空白」正是那一次失败唯一说得清的样子。
                        thinking_chars += len(reasoning)
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        pieces.append(content)
                        answer_chars += len(content)
                    total = answer_chars + thinking_chars
                    if on_progress is not None and total - last_reported >= AI_PROGRESS_STEP_CHARS:
                        last_reported = total
                        on_progress(answer_chars, thinking_chars)
                    finish = choices[0].get("finish_reason")
                    if finish:
                        usage["finish_reason"] = finish
    except AiGatewayError:
        raise
    except httpx.TimeoutException as exc:
        #: `sent=False` 是连握手都没完成；`sent=True` 是 200 之后半路断了——那时候钱可能已经花。
        raise AiGatewayError(
            f"{AI_STREAM_STALL_SECONDS:.0f} 秒没有收到一个字：{type(exc).__name__}",
            sent=sent, billed=sent,
        ) from exc
    except httpx.HTTPError as exc:
        raise AiGatewayError(f"网关没有连上：{type(exc).__name__} {exc}", sent=sent, billed=sent) from exc

    content = "".join(pieces)
    if not content.strip():
        raise AiGatewayError(
            f"上游 HTTP 200 但正文是空的（finish_reason={usage.get('finish_reason')}，"
            f"思考 {thinking_chars} 字）：推理模型把输出用完了，这一次没有结论",
            sent=True,
        )
    return content, usage


def call_model(
    messages: list[dict],
    *,
    timeout_seconds: float = AI_RUN_TIMEOUT_SECONDS,
    on_progress: Callable[[int, int], None] | None = None,
    use_response_format: bool = True,
) -> tuple[str, dict]:
    """流式跑一趟，返回 (正文, usage)。

    思考内容（`delta.reasoning` / `delta.reasoning_content`）一个字都不并进正文，
    只数它的字数：那部分不是模型给的答案，混进正文里解析 JSON 就成了抽奖。

    上游在做出结论之前把这趟挡回来（429 并发满、5xx 过载）时，按 `Retry-After` 等一会儿
    重发，最多 `AI_TRANSIENT_RETRY_ATTEMPTS` 趟。这种重发不额外花钱——一个 token 都没生成；
    而它救的是「上游明说请稍后再试」那一类本来就该再敲一次的失败。
    """
    config = ai_config()
    if not (config["base_url"] and config["model"] and config["api_key"]):
        raise not_configured()
    body: dict[str, Any] = {
        "model": config["model"],
        "temperature": 0.2,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if use_response_format:
        body["response_format"] = {"type": "json_object"}
    timeout = httpx.Timeout(
        connect=AI_STREAM_CONNECT_SECONDS,
        # 读超时管的是「多久不吐一个字算断流」，整趟的上限另算：见下面那个 deadline。
        read=AI_STREAM_STALL_SECONDS,
        write=AI_STREAM_STALL_SECONDS,
        pool=AI_STREAM_CONNECT_SECONDS,
    )
    deadline = time.monotonic() + timeout_seconds
    attempts = 0
    while True:
        attempts += 1
        try:
            return _stream_attempt(config, body, timeout, deadline, timeout_seconds, on_progress, attempts)
        except AiGatewayError as error:
            if not error.transient:
                raise
            if attempts >= AI_TRANSIENT_RETRY_ATTEMPTS:
                raise carry(
                    f"{error}（上游连着把 {attempts} 趟都挡在做结论之前，这一趟没花钱）",
                    error,
                ) from None
            #: 等待时长只认上游那一趟亲自给的 `Retry-After`（它说 0 就是 0，别自己加戏），
            #: 没给才退回退避表。
            wait = max(0.0, float(error.retry_after))
            remaining = deadline - time.monotonic()
            if remaining <= wait + 1.0:
                raise carry(
                    f"{error}；上游还要等 {wait:.0f} 秒，而这一趟只剩 {max(remaining, 0):.0f} 秒，"
                    f"不够再试一次，就此收手（已试 {attempts} 趟，都没花钱）",
                    error,
                ) from None
            logger.info(
                "上游在做结论之前把这趟挡了回来（第 %s/%s 趟），等 %.0f 秒重发：%s",
                attempts, AI_TRANSIENT_RETRY_ATTEMPTS, wait, _scrub(str(error))[:160],
            )
            time.sleep(wait)


def _merge_usage(usage: dict[str, Any], chunk: dict) -> None:
    """后面的非空值盖掉前面的：有的网关只在最后一帧给 usage。"""
    incoming = _usage_of(chunk)
    for key, value in incoming.items():
        if value is not None:
            usage[key] = value
    choices = chunk.get("choices") or []
    if choices and choices[0].get("finish_reason"):
        usage["finish_reason"] = choices[0]["finish_reason"]


def call_chat(prompt: str, *, system: str = "") -> str:
    """给每日总结那种「一问一答」用的薄壳。失败照实抛，不静默降级。

    总结只要几百字，两分钟不回话就当这一趟废了，不必按分析那种十分钟的上限等它。
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    content, _usage = call_model(messages, timeout_seconds=AI_STREAM_STALL_SECONDS)
    return content
