"""登录、企业微信测试推送与回调。"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from backend.app.core import auth
from backend.app.core.logging import get_logger

router = APIRouter()
logger = get_logger("limit_pattern.wecom")


# ---------- 访问密码 ----------
@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    need = auth.required()
    return {"required": need, "logged_in": (not need) or auth.valid(request.cookies.get(auth.COOKIE)), "headless": auth.headless()}


@router.post("/auth/login")
def auth_login(body: dict, request: Request, response: Response) -> dict:
    if not auth.check_password(str((body or {}).get("password") or "")):
        raise HTTPException(status_code=401, detail="密码不对")
    response.set_cookie(auth.COOKIE, auth.issue(), max_age=auth.MAX_AGE, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return {"ok": True}


@router.post("/auth/logout")
def auth_logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


# ---------- 企业微信 ----------
@router.post("/wecom/test")
def wecom_test(body: dict | None = None) -> dict:
    from backend.app import __version__
    from backend.app.core.clock import now
    from backend.app.services.wecom import client

    cfg = client.config()
    if not client.ready(cfg):
        raise HTTPException(status_code=422, detail="企业微信没配置完整：需要 企业ID + AgentId + Secret（或群机器人 webhook）")
    channel = (body or {}).get("channel") or "all"
    if channel == "webhook":
        cfg = dict(cfg, secret="")
    elif channel == "app":
        cfg = dict(cfg, webhook="")
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    msg = client.markdown(f"**涨停形态系统 测试消息**\n> 时间：{stamp}\n> 版本：v{__version__}\n收到这条说明推送配置正确。")
    try:
        used = client.send(*msg, cfg=cfg)
    except client.WeComError as exc:
        return {"ok": False, "errcode": exc.code, "message": str(exc)}
    return {"ok": True, "errcode": 0, "channels": used, "message": "已发送：" + "、".join({"app": "应用消息", "webhook": "群机器人"}[u] for u in used)}


def _crypt():
    from backend.app.services.wecom import client
    from backend.app.services.wecom.crypto import CryptoUnavailable, WeComCryptoError, WXBizMsgCrypt

    cfg = client.config()
    if not (cfg["enabled"] and cfg["token"] and cfg["aes_key"] and cfg["corpid"]):
        raise HTTPException(status_code=404, detail="企业微信接收消息没有开启")
    try:
        return cfg, WXBizMsgCrypt(cfg["token"], cfg["aes_key"], cfg["corpid"])
    except CryptoUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except WeComCryptoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/wecom/callback")
def wecom_verify(msg_signature: str = Query(""), timestamp: str = Query(""), nonce: str = Query(""), echostr: str = Query("")):
    from backend.app.services.wecom.crypto import WeComCryptoError

    _cfg, crypt = _crypt()
    try:
        return PlainTextResponse(crypt.verify_url(msg_signature, timestamp, nonce, echostr))
    except WeComCryptoError as exc:
        logger.warning("企业微信 URL 验证失败：%s", exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def handle_message(cfg: dict, xml_text: str) -> dict:
    """解析解密后的消息，返回 {user, cmd, reply}；reply 通过应用消息异步发回。"""
    from backend.app.services.wecom import commands
    from backend.app.services.wecom.crypto import extract

    user = extract(xml_text, "FromUserName")
    if extract(xml_text, "MsgType") != "text":
        return {"user": user, "cmd": None, "reply": None}
    content = extract(xml_text, "Content")
    if not commands.allowed(user, cfg["admins"]):
        return {"user": user, "cmd": None, "reply": "你不在本系统的指令白名单里，请让管理员在 设置 → 企业微信 → 指令白名单 里添加你的账号。"}
    cmd, arg = commands.parse(content)
    return {"user": user, "cmd": cmd or "unknown", "arg": arg}


def _reply_async(cfg: dict, info: dict) -> None:
    from backend.app.services.wecom import client, commands

    def run():
        try:
            reply = info.get("reply")
            if reply is None and info.get("cmd"):
                reply = commands.run("" if info["cmd"] == "unknown" else info["cmd"], info.get("arg", ""))
            if reply:
                client.send_app(cfg, *client.text(reply), touser=info["user"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("企业微信指令回复失败：%s", exc)

    threading.Thread(target=run, name="wecom-reply", daemon=True).start()


@router.post("/wecom/callback")
async def wecom_receive(request: Request, msg_signature: str = Query(""), timestamp: str = Query(""), nonce: str = Query("")):
    from backend.app.services.wecom.crypto import WeComCryptoError

    cfg, crypt = _crypt()
    body = (await request.body()).decode("utf-8", "replace")
    try:
        plain = crypt.decrypt_msg(body, msg_signature, timestamp, nonce)
        info = handle_message(cfg, plain)
    except WeComCryptoError as exc:
        logger.warning("企业微信回调校验失败：%s", exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if info.get("user") and (info.get("reply") or info.get("cmd")) and cfg["agentid"] and cfg["secret"]:
        _reply_async(cfg, info)
    return PlainTextResponse("success")  # 5 秒内必须应答；结果用应用消息异步回给发送者
