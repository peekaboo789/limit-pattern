"""个人微信 iLink Bot / ClawBot 适配器。

协议（可替换 base_url，默认官方 iLink 接口）：
- POST {base}/get_bot_qrcode：返回二维码内容（dataURL 或文本链接），设置页显示后手机扫码。
- POST {base}/get_qrcode_status：轮询扫码状态，返回 token。
- POST {base}/getupdates：context_token 长轮询收到文本消息。
- POST {base}/sendmessage：发纯文本。

所有 token / context_token 只存在内存和 ./data，绝不写日志。非官方/自托管网关
同样走这个接口形状，因此能在设置页替换 base_url。
"""

from __future__ import annotations

import base64
import json
import threading
import time as _time
from pathlib import Path

import httpx

from backend.app.core.config import data_dir, read_settings_file
from backend.app.core.logging import get_logger
from backend.app.services.wecom.client import split_text

logger = get_logger("limit_pattern.wechat")

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
TIMEOUT = 15.0
LONG_POLL_TIMEOUT = 70.0
RETRIES = 2


class WeChatError(RuntimeError):
    def __init__(self, msg: str, code: str | int | None = None):
        super().__init__(msg)
        self.code = code


def config(raw: dict | None = None) -> dict:
    raw = read_settings_file() if raw is None else raw
    return {
        "enabled": raw.get("wechat_enabled") is True,
        "base": (str(raw.get("wechat_api_base") or "").strip() or DEFAULT_BASE).rstrip("/"),
        "token": str(raw.get("wechat_token") or "").strip(),
        "username": str(raw.get("wechat_username") or "").strip(),
        "admins": [a.strip() for a in str(raw.get("wechat_admins") or "").replace("，", ",").replace("|", ",").split(",") if a.strip()],
        "events": list(raw.get("wechat_events") or []),
    }


def ready(cfg: dict) -> bool:
    # token 只写不读；是否就绪看会话文件（扫码成功后持久化到 ./data）。
    return bool(cfg["token"]) or _session_file().exists()


_HTTP: dict = {"client": None}
_STATE_LOCK = threading.Lock()
_STATE: dict = {"context_token": None, "token": None, "bot_id": None, "username": None, "qrcode": None}


def set_http_client(client: httpx.Client | None) -> None:
    _HTTP["client"] = client


def _http() -> httpx.Client:
    if _HTTP["client"] is None:
        _HTTP["client"] = httpx.Client(timeout=TIMEOUT, trust_env=False)
    return _HTTP["client"]


def _session_file() -> Path:
    return data_dir() / "wechat_session.json"


def _load_session() -> dict:
    try:
        return json.loads(_session_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_session(data: dict) -> None:
    path = _session_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _post(base: str, endpoint: str, payload: dict, timeout: float = TIMEOUT) -> dict:
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            r = _http().post(f"{base}/{endpoint}", content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             headers={"Content-Type": "application/json; charset=utf-8"}, timeout=timeout)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            try:
                data = r.json()
            except ValueError:
                data = {"raw": r.text, "http_status": r.status_code}
            return data
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last = exc
            _time.sleep(0.4 * (attempt + 1))
    raise WeChatError(f"连不上微信 iLink 接口（{type(last).__name__}）：检查网络或 base_url")


def _pick_token(resp: dict) -> str:
    for key in ("token", "access_token", "context_token", "session_token"):
        if resp.get(key):
            return str(resp[key])
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    for key in ("token", "access_token", "context_token", "session_token"):
        if data.get(key):
            return str(data[key])
    return ""


def _pick_qrcode(resp: dict) -> str:
    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    for key in ("qrcode", "qr_code", "qrcode_url", "qr_url", "url"):
        if data.get(key):
            value = str(data[key])
            if value.startswith("data:image") or value.startswith("http"):
                return value
    return ""


def get_qrcode(cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    resp = _post(cfg["base"], "get_bot_qrcode", {})
    qr = _pick_qrcode(resp)
    if not qr and resp.get("errcode"):
        raise WeChatError(str(resp.get("errmsg") or "获取二维码失败"), resp.get("errcode"))
    if not qr:
        qr = _qr_from_raw(resp)
    with _STATE_LOCK:
        _STATE["qrcode"] = qr
    return {"qrcode": qr, "expires_seconds": int(resp.get("expires_in") or resp.get("expires_seconds") or 120)}


def _qr_from_raw(resp: dict) -> str:
    for value in resp.values():
        if isinstance(value, str) and (value.startswith("data:image") or value.startswith("http")):
            return value
    return ""


def qr_status(cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    resp = _post(cfg["base"], "get_qrcode_status", {}, timeout=40.0)
    token = _pick_token(resp) or str(resp.get("session") or "")
    code = resp.get("code", resp.get("status", resp.get("errcode", "")))
    status = str(code).lower()
    is_ok = status in ("0", "ok", "success", "confirmed", "logged_in", "scanned") or bool(token)
    if token:
        with _STATE_LOCK:
            _STATE["token"] = token
            _STATE["context_token"] = _STATE.get("context_token") or resp.get("context_token") or ""
            _STATE["bot_id"] = resp.get("bot_id") or resp.get("wxid") or ""
            _STATE["username"] = resp.get("username") or cfg.get("username") or ""
        _save_session({k: _STATE[k] for k in ("token", "context_token", "bot_id", "username") if _STATE.get(k)})
    return {"logged_in": is_ok, "status": status, "qrcode": _STATE.get("qrcode", "")}


def _session_token(cfg: dict) -> str:
    with _STATE_LOCK:
        if _STATE.get("token"):
            return _STATE["token"]
    saved = _load_session()
    if saved.get("token"):
        with _STATE_LOCK:
            _STATE.update({k: saved.get(k) for k in ("token", "context_token", "bot_id", "username")})
        return saved["token"]
    return cfg["token"]


def _extract_message(resp: dict) -> list[dict]:
    """兼容多种常见返回形状：{data:{messages:[...]}} / {updates:[...]} / {result:[...]}。"""
    data = resp.get("data") if isinstance(resp.get("data"), (dict, list)) else resp
    updates = []
    if isinstance(data, dict):
        updates = data.get("messages") or data.get("updates") or data.get("items") or []
    elif isinstance(data, list):
        updates = data
    if not updates:
        updates = resp.get("messages") or resp.get("updates") or resp.get("result") or []
    if isinstance(updates, dict):
        updates = updates.get("messages") or updates.get("updates") or []
    out = []
    for item in updates or []:
        if not isinstance(item, dict):
            continue
        msg = item.get("message") if isinstance(item.get("message"), dict) else item
        text = msg.get("text") or msg.get("content") or item.get("text") or ""
        if not text:
            continue
        user = msg.get("from") or msg.get("from_user") or msg.get("sender") or item.get("from_user") or ""
        out.append({"user": str(user), "text": str(text)})
    return out


def getupdates(cfg: dict, long_poll: bool = True) -> list[dict]:
    token = _session_token(cfg)
    if not token:
        raise WeChatError("还没登录：先到设置页扫码")
    with _STATE_LOCK:
        context = _STATE.get("context_token") or ""
    payload = {"token": token}
    if context:
        payload["context_token"] = context
    resp = _post(cfg["base"], "getupdates", payload, timeout=LONG_POLL_TIMEOUT if long_poll else TIMEOUT)
    new_context = resp.get("context_token") or ""
    if new_context:
        with _STATE_LOCK:
            _STATE["context_token"] = new_context
        _save_session({k: _STATE[k] for k in ("token", "context_token", "bot_id", "username") if _STATE.get(k)})
    return _extract_message(resp)


def sendmessage(cfg: dict, text: str, touser: str | None = None) -> dict:
    token = _session_token(cfg)
    if not token:
        raise WeChatError("还没登录：先到设置页扫码")
    payload: dict = {"token": token, "content": text}
    if touser:
        payload["to"] = touser
        payload["touser"] = touser
    resp = _post(cfg["base"], "sendmessage", payload)
    code = resp.get("errcode", resp.get("code", 0))
    if code not in (0, "0", None, "ok", "success") and str(code).lower() not in ("ok", "success"):
        raise WeChatError(str(resp.get("errmsg") or resp.get("message") or "发送失败"), code)
    return resp


def deliver(title: str, body: str, *, touser: str | None = None, cfg: dict | None = None) -> list[str]:
    """纯文本发送：标题 + 正文合并，长文本分段。"""
    cfg = cfg or config()
    content = f"{title}\n{body}" if title else body
    used = []
    for part in split_text(content, limit=1800):
        sendmessage(cfg, part, touser=touser)
        used.append("wechat")
    return used
