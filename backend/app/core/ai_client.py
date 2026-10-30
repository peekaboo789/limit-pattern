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


def chat(system: str, prompt: str, *, settings: dict, read_timeout: float = 120.0, max_tokens: int | None = None) -> str:
    url = str(settings.get("ai_base_url") or "")
    key = str(settings.get("ai_api_key") or "")
    model = str(settings.get("ai_model") or "")
    if not (url and key and model):
        raise AiNotConfigured("未配置AI接口")
    endpoint, _note = normalize_endpoint(url)
    body: dict = {
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    if (urlparse(endpoint).hostname or "").endswith("aliyuncs.com"):
        # 百炼的 qwen3 系列非流式调用要关掉思考模式，否则报错或很慢
        body["enable_thinking"] = False
    import os

    env_proxy = any(os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"))
    proxy = None if env_proxy else system_proxy(endpoint)
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=read_timeout, write=30.0, pool=10.0)
    try:
        with httpx.Client(timeout=timeout, verify=_ssl_context(), trust_env=True, proxy=proxy) as client:
            response = client.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=body)
    except httpx.HTTPError as exc:
        raise AiFailed(describe_error(exc, endpoint, read_timeout, proxy)) from exc
    except ssl.SSLError as exc:
        raise AiFailed(describe_error(exc, endpoint, read_timeout, proxy)) from exc
    if response.status_code >= 400:
        detail = scrub(response.text, key)[:300]
        hint = ""
        if response.status_code == 401:
            hint = "（密钥无效，或密钥和地址不属于同一地域/业务空间）"
        elif response.status_code == 404:
            hint = "（地址路径不对，或模型名不存在）"
        elif response.status_code == 429:
            hint = "（请求太频繁或额度用完）"
        raise AiFailed(f"AI 接口返回 HTTP {response.status_code}{hint}：{detail}（调用地址 {endpoint}）")
    try:
        payload = response.json()
        return str(payload["choices"][0]["message"]["content"])
    except Exception as exc:
        raise AiFailed(f"AI 接口返回的内容看不懂：{scrub(response.text, key)[:300]}") from exc


def test_ai(settings: dict) -> dict:
    endpoint, note = normalize_endpoint(str(settings.get("ai_base_url") or ""))
    began = time.monotonic()
    try:
        reply = chat("你是测试助手。", "只回复：连接成功", settings=settings, read_timeout=30.0, max_tokens=20)
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
    }
