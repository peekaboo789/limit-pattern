"""企业微信发消息：access_token 缓存与刷新、message/send、错误码中文说明、限流、去重；可选群机器人 webhook。

所有密钥只从设置文件读，绝不写日志。"""

from __future__ import annotations

import json
import threading
import time as _time
from datetime import date

import httpx

from backend.app.core.clock import now
from backend.app.core.config import data_dir, read_settings_file
from backend.app.core.logging import get_logger

logger = get_logger("limit_pattern.wecom")
# httpx 在 INFO 级会打印完整 URL（含 corpsecret / access_token），这里压到 WARNING
import logging as _logging

_logging.getLogger("httpx").setLevel(_logging.WARNING)
DEFAULT_BASE = "https://qyapi.weixin.qq.com"
TIMEOUT = 10.0
RETRIES = 2
MIN_INTERVAL = 0.35  # 每秒约 3 条，远低于企业微信应用消息频率上限
TOKEN_ERRORS = {40014, 42001, 40001, 41001}

ERRORS = {
    0: "成功",
    -1: "企业微信系统繁忙，稍后再试",
    40001: "Secret 不对（获取 access_token 时 secret 错误，或 access_token 无效）",
    40013: "企业ID（corpid）不对",
    40014: "access_token 无效（已自动刷新重试）",
    40056: "AgentId 不对",
    41001: "缺少 access_token",
    42001: "access_token 已过期（已自动刷新重试）",
    40003: "touser 里有无效的成员账号",
    81013: "接收人（touser / toparty / totag）全部无效或不在应用可见范围",
    82001: "指定的接收人都不在应用可见范围",
    301002: "无权限操作指定的应用（AgentId 与 Secret 不是同一个应用？）",
    48002: "API 接口无权限调用（应用没开通对应权限）",
    60020: "服务器 IP 不在应用的「企业可信IP」里：在管理后台 应用管理 → 本应用 → 企业可信IP 添加本机公网 IP，"
           "或在设置里填「代理地址」，让请求经由已加白名单的服务器转发",
    45009: "接口调用超过频率限制，稍后再试",
    45033: "接口并发调用超过限制",
    44004: "消息内容为空",
    45002: "消息内容超过长度限制",
    93000: "群机器人 webhook 地址无效",
    93004: "群机器人已被移出群",
    45004: "群机器人消息内容过长或格式不对",
}


def explain(code: int | None, msg: str = "") -> str:
    if code is None:
        return msg or "未知错误"
    text = ERRORS.get(int(code))
    return f"{text}（errcode {code}）" if text else f"企业微信返回 errcode {code}：{msg}"


class WeComError(RuntimeError):
    def __init__(self, code: int | None, msg: str):
        super().__init__(explain(code, msg))
        self.code = code
        self.raw = msg


def config(raw: dict | None = None) -> dict:
    raw = read_settings_file() if raw is None else raw
    return {
        "enabled": raw.get("wecom_enabled") is True,
        "corpid": str(raw.get("wecom_corpid") or "").strip(),
        "agentid": str(raw.get("wecom_agentid") or "").strip(),
        "secret": str(raw.get("wecom_secret") or "").strip(),
        "base": (str(raw.get("wecom_proxy") or "").strip() or DEFAULT_BASE).rstrip("/"),
        "touser": str(raw.get("wecom_touser") or "").strip(),
        "toparty": str(raw.get("wecom_toparty") or "").strip(),
        "totag": str(raw.get("wecom_totag") or "").strip(),
        "token": str(raw.get("wecom_token") or "").strip(),
        "aes_key": str(raw.get("wecom_aes_key") or "").strip(),
        "admins": [a.strip() for a in str(raw.get("wecom_admins") or "").replace("，", ",").replace("|", ",").split(",") if a.strip()],
        "events": list(raw.get("wecom_events") or []),
        "webhook": str(raw.get("wecom_webhook") or "").strip(),
    }


def ready(cfg: dict) -> bool:
    return bool(cfg["corpid"] and cfg["agentid"] and cfg["secret"]) or bool(cfg["webhook"])


_HTTP: dict = {"client": None}


def set_http_client(client: httpx.Client | None) -> None:
    """测试用：注入 httpx.Client(transport=MockTransport(...))。"""
    _HTTP["client"] = client
    _TOKEN.update(value=None, expires=0.0, key=None)


def _http() -> httpx.Client:
    if _HTTP["client"] is None:
        _HTTP["client"] = httpx.Client(timeout=TIMEOUT, trust_env=False)
    return _HTTP["client"]


_TOKEN: dict = {"value": None, "expires": 0.0, "key": None}
_TOKEN_LOCK = threading.Lock()
_SEND_LOCK = threading.Lock()
_LAST_SEND = {"at": 0.0}


def access_token(cfg: dict, force: bool = False) -> str:
    key = (cfg["base"], cfg["corpid"], cfg["secret"])
    with _TOKEN_LOCK:
        if not force and _TOKEN["key"] == key and _TOKEN["value"] and _time.monotonic() < _TOKEN["expires"]:
            return _TOKEN["value"]
        resp = _request("GET", f"{cfg['base']}/cgi-bin/gettoken", params={"corpid": cfg["corpid"], "corpsecret": cfg["secret"]})
        if resp.get("errcode", 0) != 0 or not resp.get("access_token"):
            raise WeComError(resp.get("errcode"), str(resp.get("errmsg") or ""))
        ttl = int(resp.get("expires_in") or 7200)
        _TOKEN.update(value=resp["access_token"], expires=_time.monotonic() + max(60, ttl - 300), key=key)
        return _TOKEN["value"]


def _request(method: str, url: str, **kwargs) -> dict:
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            r = _http().request(method, url, **kwargs)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
            last = exc
            _time.sleep(0.5 * (attempt + 1))
    raise WeComError(None, f"连不上企业微信接口（{type(last).__name__}）：检查网络或代理地址")


def _throttle() -> None:
    with _SEND_LOCK:
        wait = MIN_INTERVAL - (_time.monotonic() - _LAST_SEND["at"])
        if wait > 0:
            _time.sleep(wait)
        _LAST_SEND["at"] = _time.monotonic()


def send_app(cfg: dict, msgtype: str, body: dict, touser: str | None = None) -> dict:
    payload = {"msgtype": msgtype, "agentid": int(cfg["agentid"]), msgtype: body, "safe": 0,
               "enable_duplicate_check": 1, "duplicate_check_interval": 600}
    target = touser if touser is not None else cfg["touser"]
    if target:
        payload["touser"] = target
    if touser is None and cfg["toparty"]:
        payload["toparty"] = cfg["toparty"]
    if touser is None and cfg["totag"]:
        payload["totag"] = cfg["totag"]
    if not any(payload.get(k) for k in ("touser", "toparty", "totag")):
        payload["touser"] = "@all"
    for attempt in range(2):
        token = access_token(cfg, force=attempt > 0)
        _throttle()
        resp = _request("POST", f"{cfg['base']}/cgi-bin/message/send", params={"access_token": token},
                        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        headers={"Content-Type": "application/json; charset=utf-8"})
        code = int(resp.get("errcode", 0) or 0)
        if code in TOKEN_ERRORS and attempt == 0:
            continue  # access_token 失效：刷新后重发一次
        if code != 0:
            raise WeComError(code, str(resp.get("errmsg") or ""))
        return resp
    raise WeComError(42001, "access_token 刷新后仍无效")


def send_webhook(cfg: dict, msgtype: str, body: dict) -> dict:
    _throttle()
    kind = "markdown" if msgtype in ("markdown", "textcard") else "text"
    content = body.get("content") or f"**{body.get('title', '')}**\n{body.get('description', '')}"
    resp = _request("POST", cfg["webhook"], content=json.dumps({"msgtype": kind, kind: {"content": content[:4000]}}, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8"})
    if int(resp.get("errcode", 0) or 0) != 0:
        raise WeComError(resp.get("errcode"), str(resp.get("errmsg") or ""))
    return resp


def send(msgtype: str, body: dict, *, touser: str | None = None, cfg: dict | None = None) -> list[str]:
    """按设置发：应用消息和/或群机器人。返回实际用到的通道。"""
    cfg = cfg or config()
    used = []
    if cfg["corpid"] and cfg["agentid"] and cfg["secret"]:
        send_app(cfg, msgtype, body, touser)
        used.append("app")
    if cfg["webhook"] and touser is None:
        send_webhook(cfg, msgtype, body)
        used.append("webhook")
    if not used:
        raise WeComError(None, "企业微信没配置完整：需要 企业ID + AgentId + Secret，或群机器人 webhook")
    return used


def text(content: str) -> tuple[str, dict]:
    return "text", {"content": content[:2000]}


def markdown(content: str) -> tuple[str, dict]:
    return "markdown", {"content": content[:4000]}


# ---------- 去重（每天每只股票每个阈值只推一次） ----------
_DEDUPE_LOCK = threading.Lock()


def _dedupe_path():
    return data_dir() / "wecom_sent.json"


def once(key: str, day: date | None = None) -> bool:
    """第一次见到 key 返回 True 并记下；按天清理。"""
    day_s = (day or now().date()).isoformat()
    with _DEDUPE_LOCK:
        try:
            data = json.loads(_dedupe_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if data.get("date") != day_s:
            data = {"date": day_s, "keys": []}
        if key in data["keys"]:
            return False
        data["keys"].append(key)
        path = _dedupe_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return True
