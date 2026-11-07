"""个人微信 iLink Bot / ClawBot 适配器（严格按官方 openclaw-weixin 协议）。

协议来自官方插件源码 /workspace/ilink-ref/package/src（@tencent-weixin/openclaw-weixin 2.4.10）：

- 登录二维码
  POST https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3
  body: {"local_token_list": []}
  -> {"qrcode":"<id>","qrcode_img_content":"https://liteapp.weixin.qq.com/q/...","ret":0}

- 登录状态（长轮询，用 GET）
  GET  https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode=<id>
  -> {"status":"wait|scaned|confirmed|expired|...","bot_token":"...",
      "ilink_bot_id":"...","ilink_user_id":"...","baseurl":"..."}

- 拉消息（长轮询）
  POST {baseurl}/ilink/bot/getupdates
  body: {"get_updates_buf": <base64 cursor>,"base_info":{...}}
  带 Authorization: Bearer <bot_token>；返回 msgs[] 与新的 get_updates_buf。

- 发消息
  POST {baseurl}/ilink/bot/sendmessage
  body: {"msg":{"from_user_id":"","to_user_id":<ilink_user_id>,
    "client_id":<随机字符串>,"message_type":2,"message_state":2,
    "item_list":[{"type":1,"text_item":{"text":"..."}}],
    "context_token":<对方最近消息的 context_token>},
    "base_info":{...}}

- 推送必须先有对方最近的 context_token（只能回复发过消息的用户）。首个发消息的人自动绑定为管理员，
  其他用户消息忽略；绑定持久化到 data/wechat_session.json，重新扫码登录不会自动清除绑定。
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from backend.app.core.config import data_dir, read_settings_file
from backend.app.core.logging import get_logger

logger = get_logger("limit_pattern.wechat")

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
FIXED_QR_BASE = DEFAULT_BASE
BOT_TYPE = "3"
TIMEOUT = 15.0
QR_LONG_POLL_TIMEOUT = 35.0
LONG_POLL_TIMEOUT = 70.0
RETRIES = 2

# 官方包里的常量：app 自述标识。这里只做兼容，不能写密钥。
CHANNEL_VERSION = "2.4.10"
ILINK_APP_CLIENT_VERSION = "0x00020000"

# 消息类型与状态（与官方 proto/常量一致）
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
MESSAGE_ITEM_TEXT = 1

STALE_TOKEN_ERRCODE = -14


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
    return bool(cfg["token"]) or bool(_load_session().get("token"))


_HTTP: dict = {"client": None}
_STATE_LOCK = threading.Lock()
_STATE: dict = {
    "context_token": None,
    "token": None,
    "baseurl": None,
    "bot_id": None,
    "ilink_user_id": None,
    "username": None,
    "qrcode": None,
    "qrcode_img_content": None,
    "status": None,
    "admin": None,
    "admin_bound_at": None,
    "context_tokens": {},
}


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


def _persist_session_locked() -> None:
    """把内存里的协议状态和绑定管理员的非敏感部分写盘。token 在内存/文件都可，但绝不写日志。"""
    payload = {
        "token": _STATE.get("token") or "",
        "baseurl": _STATE.get("baseurl") or "",
        "bot_id": _STATE.get("bot_id") or "",
        "ilink_user_id": _STATE.get("ilink_user_id") or "",
        "username": _STATE.get("username") or "",
        "get_updates_buf": _STATE.get("get_updates_buf") or "",
        "admin": _STATE.get("admin") or "",
        "admin_bound_at": _STATE.get("admin_bound_at") or "",
        "context_tokens": _STATE.get("context_tokens") or {},
    }
    _save_session(payload)


def _sync_state_from_session(saved: dict) -> None:
    with _STATE_LOCK:
        for key in ("token", "baseurl", "bot_id", "ilink_user_id", "username", "get_updates_buf", "admin", "admin_bound_at"):
            if saved.get(key):
                _STATE[key] = saved[key]
        if isinstance(saved.get("context_tokens"), dict):
            _STATE["context_tokens"] = saved["context_tokens"]
        if saved.get("token"):
            _STATE["token"] = saved["token"]


def _load_state() -> dict:
    saved = _load_session()
    _sync_state_from_session(saved)
    return saved


def _current_base(cfg: dict) -> str:
    with _STATE_LOCK:
        baseurl = _STATE.get("baseurl") or ""
    return baseurl.rstrip("/") if baseurl else cfg["base"]


def _current_token(cfg: dict) -> str:
    with _STATE_LOCK:
        token = _STATE.get("token") or ""
    if token:
        return token
    _load_state()
    with _STATE_LOCK:
        return _STATE.get("token") or cfg["token"]


def _build_headers(token: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode("utf-8")).decode("ascii"),
        "iLink-App-Id": "",
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": "OpenClaw"}


def _post(base: str, endpoint: str, payload: dict, *, token: str | None = None, timeout: float = TIMEOUT) -> dict:
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            r = _http().post(f"{base}/{endpoint}", content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             headers=_build_headers(token), timeout=timeout)
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


def _get(base: str, endpoint: str, *, timeout: float = TIMEOUT) -> dict:
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            r = _http().get(f"{base}/{endpoint}", headers=_build_headers(), timeout=timeout)
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


def _code_ok(resp: dict) -> bool:
    if resp.get("ret") not in (None, 0, "0"):
        return False
    if resp.get("errcode") not in (None, 0, "0"):
        return False
    return True


def _raise_if_error(resp: dict, default: str = "请求失败") -> None:
    if _code_ok(resp):
        return
    code = resp.get("errcode", resp.get("ret"))
    raise WeChatError(str(resp.get("errmsg") or resp.get("message") or default), code)


def get_qrcode(cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    if not cfg["enabled"]:
        raise WeChatError("先启用个人微信并保存设置，再获取二维码")
    resp = _post(FIXED_QR_BASE, f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}", {"local_token_list": []})
    _raise_if_error(resp, "获取二维码失败")
    qr_id = str(resp.get("qrcode") or "")
    qr_url = str(resp.get("qrcode_img_content") or "")
    if not qr_id:
        raise WeChatError("微信接口没返回 qrcode")
    qr = qr_url or f"https://liteapp.weixin.qq.com/q/?qrcode={qr_id}&bot_type={BOT_TYPE}"
    with _STATE_LOCK:
        _STATE["qrcode"] = qr_id
        _STATE["qrcode_img_content"] = qr_url
        _STATE["status"] = "wait"
    return {"qrcode": qr, "qrcode_id": qr_id, "qrcode_img_content": qr_url, "expires_seconds": 300}


def qr_status(cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    with _STATE_LOCK:
        qr_id = _STATE.get("qrcode") or ""
        baseurl = _STATE.get("baseurl") or FIXED_QR_BASE
    if not qr_id:
        # 已经登录过了：直接报已登录；还没有二维码则返回未登录，便于页面初始加载。
        if _current_token(cfg):
            with _STATE_LOCK:
                _STATE["status"] = "confirmed"
            return {"logged_in": True, "status": "confirmed", "qrcode": ""}
        return {"logged_in": False, "status": "wait", "qrcode": ""}

    # 官方 get_qrcode_status 是 GET 长轮询；二维码阶段用固定官方域名/重定向后的 host。
    try:
        from urllib.parse import quote

        resp = _get(baseurl.rstrip("/"), f"ilink/bot/get_qrcode_status?qrcode={quote(qr_id)}", timeout=QR_LONG_POLL_TIMEOUT)
    except Exception:
        resp = None
    if resp is None:
        return {"logged_in": False, "status": "wait", "qrcode": qr_id}

    status = str(resp.get("status") or "wait").lower()
    with _STATE_LOCK:
        _STATE["status"] = status
    if status == "confirmed":
        token = str(resp.get("bot_token") or "")
        bot_id = str(resp.get("ilink_bot_id") or "")
        user_id = str(resp.get("ilink_user_id") or "")
        baseurl = str(resp.get("baseurl") or _current_base(cfg))
        with _STATE_LOCK:
            _STATE["token"] = token
            _STATE["bot_id"] = bot_id
            _STATE["ilink_user_id"] = user_id
            _STATE["baseurl"] = baseurl
            _STATE["username"] = _STATE.get("username") or cfg.get("username") or ""
            _persist_session_locked()
        return {"logged_in": True, "status": status, "qrcode": "", "baseurl": baseurl,
                "bot_id": bot_id, "ilink_user_id": user_id}
    return {"logged_in": False, "status": status, "qrcode": qr_id,
            "baseurl": resp.get("baseurl") or _current_base(cfg)}


def _extract_updates(resp: dict) -> list[dict]:
    """官方 getUpdates 返回 msgs: WeixinMessage[]。只取文本。"""
    msgs = resp.get("msgs") or []
    out = []
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        user = str(msg.get("from_user_id") or "")
        text = ""
        item_list = msg.get("item_list") or []
        for item in item_list or []:
            if not isinstance(item, dict):
                continue
            if int(item.get("type") or 0) == MESSAGE_ITEM_TEXT:
                text_item = item.get("text_item") or {}
                text = str(text_item.get("text") or "")
                if text:
                    break
        if not text:
            continue
        out.append({"user": user, "text": text, "context_token": msg.get("context_token") or ""})
    return out


def getupdates(cfg: dict, long_poll: bool = True) -> list[dict]:
    _load_state()
    token = _current_token(cfg)
    if not token:
        raise WeChatError("还没登录：先到设置页扫码")
    base = _current_base(cfg)
    with _STATE_LOCK:
        buf = _STATE.get("get_updates_buf") or ""
    payload = {"get_updates_buf": buf, "base_info": _base_info()}
    resp = _post(base, "ilink/bot/getupdates", payload, token=token, timeout=LONG_POLL_TIMEOUT if long_poll else TIMEOUT)
    _raise_if_error(resp, "获取微信消息失败")
    new_buf = resp.get("get_updates_buf") or buf
    with _STATE_LOCK:
        _STATE["get_updates_buf"] = new_buf
        for item in _extract_updates(resp):
            if item["user"] and item["context_token"]:
                _STATE.setdefault("context_tokens", {})[item["user"]] = item["context_token"]
        _persist_session_locked()
    return _extract_updates(resp)


def current_context_token(cfg: dict | None = None, touser: str | None = None) -> str:
    """返回目标用户最近一条入站消息的 context_token；没有则返回空串。"""
    cfg = cfg or config()
    _load_state()
    with _STATE_LOCK:
        admin = _STATE.get("admin") or (cfg.get("admins") or [None])[0] or ""
        user = touser or admin
        tokens = _STATE.get("context_tokens") or {}
        return str(tokens.get(user) or "")


def current_admin_bound(cfg: dict | None = None) -> bool:
    """是否有已绑定的管理员。current_admin() 会 fallback 到配置白名单，这里返回真实绑定状态。"""
    cfg = cfg or config()
    _load_state()
    with _STATE_LOCK:
        return bool(_STATE.get("admin"))


def sendmessage(cfg: dict, text: str, touser: str | None = None, context_token: str | None = None) -> dict:
    _load_state()
    token = _current_token(cfg)
    if not token:
        raise WeChatError("还没登录：先到设置页扫码")
    base = _current_base(cfg)
    if not touser:
        with _STATE_LOCK:
            touser = _STATE.get("ilink_user_id") or ""
    if not touser:
        raise WeChatError("还没有可发送的用户：请先让管理员在微信里给机器人发一条消息")

    ctx = context_token
    if not ctx:
        ctx = current_context_token(cfg, touser)
    if not ctx:
        raise WeChatError("还没收到你发给机器人的消息，暂时无法主动推送——请先用你自己的微信给机器人发任意一条消息，等系统收到后再点发送测试消息。")

    item_list = [{"type": MESSAGE_ITEM_TEXT, "text_item": {"text": text}}]
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": touser,
            "client_id": secrets.token_hex(16),
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": item_list,
            "context_token": ctx or "",
        },
        "base_info": _base_info(),
    }
    resp = _post(base, "ilink/bot/sendmessage", payload, token=token)
    if "raw" in resp or "http_status" in resp:
        code = resp.get("http_status")
        raise WeChatError(f"发送失败：微信接口返回了非 JSON 响应（HTTP {code}）", code)
    stale = False
    for key in ("ret", "errcode"):
        try:
            stale = stale or int(resp.get(key)) == STALE_TOKEN_ERRCODE
        except (TypeError, ValueError):
            pass
    if stale:
        raise WeChatError("登录已失效，请重新扫码登录", STALE_TOKEN_ERRCODE)
    _raise_if_error(resp, "发送失败")
    return resp


def deliver(title: str, body: str, *, touser: str | None = None, cfg: dict | None = None) -> list[str]:
    """纯文本发送：标题 + 正文合并，长文本分段；没有管理员时主动推送会跳过。"""
    cfg = cfg or config()
    content = f"{title}\n{body}" if title else body
    admin = current_admin(cfg)
    if not touser and not admin:
        return []
    used = []
    for part in split_text(content, limit=1800):
        sendmessage(cfg, part, touser=touser or admin)
        used.append("wechat")
    return used


# ---------- 管理员：第一个发消息的人自动绑定 ----------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def current_admin(cfg: dict | None = None) -> str:
    cfg = cfg or config()
    _load_state()
    with _STATE_LOCK:
        return _STATE.get("admin") or (cfg.get("admins") or [None])[0] or ""


def admin_public(cfg: dict | None = None) -> dict:
    cfg = cfg or config()
    _load_state()
    with _STATE_LOCK:
        admin = _STATE.get("admin") or (cfg.get("admins") or [None])[0] or ""
        bound_at = _STATE.get("admin_bound_at") or ""
    return {
        "bound": bool(admin),
        "user_id": admin,
        "masked": _mask_user(admin) if admin else "",
        "bound_at": bound_at,
    }


def _mask_user(user_id: str) -> str:
    if not user_id:
        return ""
    if len(user_id) <= 4:
        return "*" * len(user_id)
    return user_id[:2] + "*" * max(2, len(user_id) - 6) + user_id[-4:]


def bind_first_sender(cfg: dict, user: str) -> bool:
    """首个发消息的人绑定为管理员。线程安全；返回本次是否完成绑定。"""
    if not user:
        return False
    _load_state()
    with _STATE_LOCK:
        if not _STATE.get("admin"):
            _STATE["admin"] = user
            _STATE["admin_bound_at"] = _now_iso()
            _persist_session_locked()
            return True
        return _STATE.get("admin") == user


def unbind_admin(cfg: dict | None = None) -> None:
    _load_state()
    with _STATE_LOCK:
        _STATE["admin"] = ""
        _STATE["admin_bound_at"] = ""
        _STATE["context_tokens"] = {}
        _persist_session_locked()


# 复用企业微信分段函数，避免重复实现；此处本地兜底。
def split_text(text: str, limit: int = 1800) -> list[str]:
    from backend.app.services.wecom.client import split_text as _split

    return _split(text, limit)
