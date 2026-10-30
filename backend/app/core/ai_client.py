"""OpenAI 兼容的 AI 接口客户端：系统代理、Windows 证书、分开的连接/读取超时、具体的中文错误。

密钥只放在请求头里，任何错误信息、日志都不带密钥。
"""

from __future__ import annotations

import re
import ssl
import time
import urllib.request
from urllib.parse import urlparse

import httpx

CONNECT_TIMEOUT = 10.0


class AiNotConfigured(RuntimeError):
    pass


class AiFailed(RuntimeError):
    pass


def normalize_endpoint(base_url: str) -> tuple[str, str]:
    """返回 (实际调用的 chat/completions 地址, 给用户的提示)。"""
    url = (base_url or "").strip().rstrip("/")
    note = ""
    if not url:
        return "", ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
        note = "地址没写 https://，已自动补上"
    if url.endswith("/chat/completions"):
        return url, note
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host.endswith("aliyuncs.com") and parsed.path in ("", "/"):
        url = url + "/compatible-mode/v1"
        note = "阿里云百炼地址缺少 /compatible-mode/v1，已自动补上"
    return url + "/chat/completions", note


def _ssl_context() -> ssl.SSLContext | bool:
    """优先用系统证书库（Windows 证书存储，兼容公司代理/杀毒软件的 HTTPS 检查），否则用 certifi。"""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return True


def system_proxy(url: str) -> str | None:
    """环境变量没有代理时，读系统代理（Windows 注册表里的 IE/系统代理设置）。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    scheme = urlparse(url).scheme or "https"
    host = urlparse(url).hostname or ""
    try:
        if urllib.request.proxy_bypass(host):
            return None
    except Exception:
        pass
    proxy = proxies.get(scheme) or proxies.get("https") or proxies.get("http")
    if proxy and not re.match(r"^[a-z]+://", proxy):
        proxy = "http://" + proxy
    return proxy or None


def scrub(text: str, key: str = "") -> str:
    text = text or ""
    if key:
        text = text.replace(key, "***")
    text = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***", text)
    return text


def describe_error(exc: BaseException, endpoint: str, read_timeout: float, proxy: str | None) -> str:
    where = f"（调用地址 {endpoint}" + (f"，经代理 {proxy}" if proxy else "") + "）"
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, httpx.ConnectTimeout):
        return f"连接超时：{int(CONNECT_TIMEOUT)} 秒内没连上服务器，检查网络或代理{where}"
    if isinstance(exc, httpx.ReadTimeout):
        return f"读取超时：{int(read_timeout)} 秒内模型没有返回结果，可能是模型太大或提示太长，可换更快的模型或稍后再试{where}"
    if isinstance(exc, (httpx.WriteTimeout, httpx.PoolTimeout)):
        return f"发送超时{where}"
    if isinstance(exc, httpx.ProxyError) or "proxy" in lower:
        return f"代理连接失败：{scrub(text)}{where}"
    if "certificate" in lower or "ssl" in lower:
        return f"SSL 证书校验失败（可能被公司代理或杀毒软件拦截 HTTPS）：{scrub(text)[:200]}{where}"
    if "getaddrinfo" in lower or "name or service not known" in lower or "nodename" in lower or "name resolution" in lower:
        return f"域名解析失败：检查地址拼写或 DNS{where}"
    if "refused" in lower or "10061" in lower:
        return f"连接被拒绝{where}"
    if "reset" in lower or "10054" in lower or "disconnected" in lower:
        return f"连接被对方断开{where}"
    return f"AI 接口没有连上：{exc.__class__.__name__} {scrub(text)[:200]}{where}"


THINKING_MODES = ("auto", "on", "off")
THINKING_READ_TIMEOUT = 300.0
_TRANSPORT = None  # 测试用：注入 httpx.MockTransport


def _learn_key(url: str, model: str) -> str:
    return f"{url.strip().rstrip('/')}|{model.strip()}"


def _learned(settings: dict, url: str, model: str) -> dict:
    table = settings.get("ai_thinking_learned") or {}
    value = table.get(_learn_key(url, model)) if isinstance(table, dict) else None
    return value if isinstance(value, dict) else {}


def _save_learned(url: str, model: str, thinking, stream: bool) -> None:
    try:
        from backend.app.core.config import _SETTINGS_LOCK, read_settings_file, write_settings_file

        with _SETTINGS_LOCK:
            current = read_settings_file()
            table = dict(current.get("ai_thinking_learned") or {})
            table[_learn_key(url, model)] = {"thinking": thinking, "stream": stream}
            current["ai_thinking_learned"] = table
            write_settings_file(current)
    except Exception:
        pass


def parse_stream(lines) -> str:
    """SSE 流：只收 delta.content，跳过 reasoning_content（思考过程）。"""
    import json as _json

    parts: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = _json.loads(data)
        except ValueError:
            continue
        for choice in chunk.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                parts.append(str(content))
    return "".join(parts)


def _content(payload: dict) -> str:
    message = payload["choices"][0]["message"]
    return str(message.get("content") or "")


def chat(system: str, prompt: str, *, settings: dict, read_timeout: float = 120.0, max_tokens: int | None = None) -> str:
    """唯一的 AI 调用入口：测试按钮、每日总结、AI 排名都走这里。"""
    url = str(settings.get("ai_base_url") or "")
    key = str(settings.get("ai_api_key") or "")
    model = str(settings.get("ai_model") or "")
    if not (url and key and model):
        raise AiNotConfigured("未配置AI接口")
    endpoint, _note = normalize_endpoint(url)
    dashscope = (urlparse(endpoint).hostname or "").endswith("aliyuncs.com")
    mode = str(settings.get("ai_thinking") or "auto")
    learned = _learned(settings, url, model)
    if mode == "on":
        thinking = True
    elif mode == "off":
        thinking = False
    else:
        thinking = learned.get("thinking")  # None = 不发送该参数
    stream = bool(learned.get("stream")) or (thinking is True and dashscope)

    import os

    env_proxy = any(os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"))
    proxy = None if env_proxy or _TRANSPORT is not None else system_proxy(endpoint)

    adjusted = False
    for _attempt in range(3):
        body: dict = {
            "model": model,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if thinking is not None:
            body["enable_thinking"] = thinking
        if stream:
            body["stream"] = True
        effective_read = max(read_timeout, THINKING_READ_TIMEOUT) if thinking else read_timeout
        timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=effective_read, write=30.0, pool=10.0)
        kwargs: dict = {"timeout": timeout, "trust_env": _TRANSPORT is None}
        if _TRANSPORT is not None:
            kwargs["transport"] = _TRANSPORT
        else:
            kwargs.update(verify=_ssl_context(), proxy=proxy)
        headers = {"Authorization": f"Bearer {key}"}
        try:
            with httpx.Client(**kwargs) as client:
                if stream:
                    with client.stream("POST", endpoint, headers=headers, json=body) as response:
                        if response.status_code >= 400:
                            response.read()
                            status, text = response.status_code, response.text
                        else:
                            content = parse_stream(response.iter_lines())
                            status, text = 200, None
                else:
                    response = client.post(endpoint, headers=headers, json=body)
                    status, text = response.status_code, response.text
                    content = None
        except (httpx.HTTPError, ssl.SSLError) as exc:
            raise AiFailed(describe_error(exc, endpoint, effective_read, proxy)) from exc

        if status == 400 and text:
            lower = text.lower()
            if "enable_thinking" in lower:
                if "true" in lower and "restricted" in lower:
                    new = True
                elif "false" in lower and "restricted" in lower:
                    new = False
                else:
                    new = not thinking if thinking is not None else True
                if new != thinking:
                    thinking = new
                    stream = bool(learned.get("stream")) or (thinking is True and dashscope)
                    adjusted = True
                    continue
            if "stream" in lower and not stream:
                stream = True
                adjusted = True
                continue
        if status >= 400:
            detail = scrub(text or "", key)[:300]
            hint = {401: "（密钥无效，或密钥和地址不属于同一地域/业务空间）", 404: "（地址路径不对，或模型名不存在）", 429: "（请求太频繁或额度用完）"}.get(status, "")
            raise AiFailed(f"AI 接口返回 HTTP {status}{hint}：{detail}（调用地址 {endpoint}）")
        if adjusted and mode == "auto":
            _save_learned(url, model, thinking, stream)
        if stream:
            if not content:
                raise AiFailed("AI 接口流式返回里没有正文内容")
            return content
        try:
            return _content(response.json())
        except Exception as exc:
            raise AiFailed(f"AI 接口返回的内容看不懂：{scrub(text or '', key)[:300]}") from exc
    raise AiFailed(f"AI 接口参数反复不兼容（调用地址 {endpoint}）")


def test_ai(settings: dict) -> dict:
    endpoint, note = normalize_endpoint(str(settings.get("ai_base_url") or ""))
    began = time.monotonic()
    try:
        reply = chat("你是测试助手。", "只回复：连接成功", settings=settings, read_timeout=60.0, max_tokens=None)
    except AiNotConfigured as exc:
        return {"ok": False, "error": str(exc), "endpoint": endpoint, "note": note, "latency_ms": 0}
    except AiFailed as exc:
        return {"ok": False, "error": str(exc), "endpoint": endpoint, "note": note, "latency_ms": int((time.monotonic() - began) * 1000)}
    return {
        "ok": True,
        "reply": reply.strip()[:200],
        "endpoint": endpoint,
        "note": note,
        "model": str(settings.get("ai_model") or ""),
        "latency_ms": int((time.monotonic() - began) * 1000),
        "thinking": _thinking_note(settings),
    }


def _thinking_note(settings: dict) -> str:
    from backend.app.core.config import read_settings_file

    mode = str(settings.get("ai_thinking") or "auto")
    if mode != "auto":
        return "开" if mode == "on" else "关"
    learned = _learned(read_settings_file(), str(settings.get("ai_base_url") or ""), str(settings.get("ai_model") or ""))
    value = learned.get("thinking")
    return "自动（未发送该参数）" if value is None else f"自动（已记住：{'开' if value else '关'}）"
