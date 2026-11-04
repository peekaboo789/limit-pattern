"""登录、企业微信测试推送与回调。"""

from __future__ import annotations

import re
import threading

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from backend.app.core import auth
from backend.app.core.logging import get_logger

router = APIRouter()
logger = get_logger("limit_pattern.wecom")


# ---------- 账号登录 ----------
def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "")


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _set_cookie(request: Request, response: Response, token: str, remember: bool) -> None:
    response.set_cookie(auth.COOKIE, token, max_age=auth.REMEMBER_AGE if remember else None, httponly=True, samesite="lax", secure=_secure(request))


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    need = auth.required()
    acc = auth.account() if need else None
    logged = (not need) or (acc is not None and auth.valid(request.cookies.get(auth.COOKIE)))
    return {"required": need, "logged_in": logged, "setup_needed": need and acc is None, "headless": auth.headless(),
            "username": acc["username"] if acc and logged else None, "from_env": bool(acc and acc["from_env"])}


@router.post("/auth/setup")
def auth_setup(body: dict, request: Request, response: Response) -> dict:
    if not auth.setup_needed():
        raise HTTPException(status_code=409, detail="已经有账号了，请直接登录")
    try:
        auth.create_account(str(body.get("username") or ""), str(body.get("password") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _set_cookie(request, response, auth.issue(request.headers.get("user-agent", ""), _ip(request)), True)
    return {"ok": True}


@router.post("/auth/login")
def auth_login(body: dict, request: Request, response: Response) -> dict:
    ip = _ip(request)
    wait = auth.locked_for(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"失败次数太多，请 {wait // 60 + 1} 分钟后再试")
    body = body or {}
    if not auth.check_credentials(str(body.get("username") or ""), str(body.get("password") or "")):
        left = auth.record_failure(ip)
        if auth.locked_for(ip):
            raise HTTPException(status_code=429, detail=f"连续失败 {auth.MAX_FAILS} 次，已锁定 {auth.LOCK_SECONDS // 60} 分钟")
        raise HTTPException(status_code=401, detail=f"用户名或密码不对（再错 {left} 次将锁定 {auth.LOCK_SECONDS // 60} 分钟）")
    auth.record_success(ip)
    remember = body.get("remember") is not False
    _set_cookie(request, response, auth.issue(request.headers.get("user-agent", ""), ip, remember), remember)
    return {"ok": True}


@router.post("/auth/logout")
def auth_logout(request: Request, response: Response) -> dict:
    auth.logout(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@router.post("/auth/logout-all")
def auth_logout_all(response: Response) -> dict:
    auth.logout_all()
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@router.get("/auth/sessions")
def auth_sessions(request: Request) -> dict:
    acc = auth.account()
    return {"username": acc["username"] if acc else None, "from_env": bool(acc and acc["from_env"]),
            "items": auth.sessions(request.cookies.get(auth.COOKIE))}


@router.post("/auth/change")
def auth_change(body: dict, request: Request, response: Response) -> dict:
    body = body or {}
    try:
        auth.change(str(body.get("current_password") or ""), body.get("username") or None, body.get("new_password") or None)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # 本设备换发新 cookie，其他设备的旧 cookie 已失效
    _set_cookie(request, response, auth.issue(request.headers.get("user-agent", ""), _ip(request)), True)
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
    fmt = (body or {}).get("format") or cfg["format"]
    try:
        used = client.deliver("✅ 涨停形态系统 测试消息", f"时间：{stamp}\n版本：v{__version__}\n格式：{FORMAT_NAMES.get(fmt, fmt)}\n收到这条说明推送配置正确。",
                              cfg=cfg, fmt=fmt)
    except client.WeComError as exc:
        return {"ok": False, "errcode": exc.code, "message": str(exc)}
    return {"ok": True, "errcode": 0, "channels": used, "message": "已发送：" + "、".join({"app": "应用消息", "webhook": "群机器人"}[u] for u in used) + f"（{FORMAT_NAMES.get(fmt, fmt)}）"}


# ---------- 个人微信 ----------
@router.get("/wechat/status")
def wechat_status() -> dict:
    from backend.app.services.bots import router

    ch = router.channel("wechat")
    cfg = ch.config()
    status = ch.qr_status(cfg)
    from backend.app.services.wechat import client as wx_client

    return {
        "enabled": cfg["enabled"],
        "ready": ch.ready(cfg),
        "logged_in": bool(status.get("logged_in")),
        "api_base": cfg["base"],
        "admins": cfg["admins"],
        "admin": wx_client.admin_public(cfg),
    }


@router.get("/wechat/qrcode")
def wechat_qrcode() -> dict:
    from backend.app.services.bots import router
    from backend.app.services.wechat.client import WeChatError

    ch = router.channel("wechat")
    cfg = ch.config()
    if not cfg["enabled"]:
        raise HTTPException(status_code=422, detail="先启用个人微信并保存设置，再获取二维码")
    try:
        from backend.app.services.wechat import client

        return client.get_qrcode(cfg)
    except WeChatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/wechat/unbind")
def wechat_unbind() -> dict:
    from backend.app.services.wechat import client as wx_client

    wx_client.unbind_admin()
    return {"ok": True, "admin": wx_client.admin_public()}


@router.post("/wechat/test")
def wechat_test() -> dict:
    from backend.app.services.bots import router
    from backend.app.services.wechat.client import WeChatError

    ch = router.channel("wechat")
    cfg = ch.config()
    if not ch.ready(cfg):
        raise HTTPException(status_code=422, detail="个人微信还没登录：请先在设置页扫码")
    try:
        result = ch.test(cfg)
    except WeChatError as exc:
        return {"ok": False, "errcode": exc.code, "message": str(exc)}
    return result


FORMAT_NAMES = {"text": "文本", "textcard": "卡片", "markdown": "Markdown"}


@router.post("/wecom/menu")
def wecom_menu() -> dict:
    from backend.app.services.wecom import client, menu

    try:
        menu.sync_menu()
    except client.WeComError as exc:
        return {"ok": False, "errcode": exc.code, "message": str(exc)}
    return {"ok": True, "errcode": 0, "message": "底部菜单已同步到企业微信（客户端重新进入应用对话即可看到）", "menu": menu.menu_json()}


@router.get("/wecom/menu")
def wecom_menu_preview() -> dict:
    from backend.app.services.wecom import menu

    return menu.menu_json()


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
    """解析解密后的消息（文字指令或菜单点击），返回 {user, cmd, arg} 或 {user, reply}。"""
    from backend.app.services.wecom import commands
    from backend.app.services.wecom.crypto import extract

    user = extract(xml_text, "FromUserName")
    kind = extract(xml_text, "MsgType")
    if kind == "event":
        if extract(xml_text, "Event").lower() != "click":
            return {"user": user, "cmd": None, "reply": None}  # 进入应用 / 查看菜单等事件不回复
        key = extract(xml_text, "EventKey")
        cmd, arg = commands.MENU.get(key, ""), ""
        if not cmd:
            return {"user": user, "cmd": None, "reply": None}
    elif kind == "text":
        content = extract(xml_text, "Content").strip()
        if re.fullmatch(r"[0-9]{1,3}", content) and commands.pending_pick(user):
            cmd, arg = "选AI", content
        else:
            cmd, arg = commands.parse(content)
    else:
        return {"user": user, "cmd": None, "reply": None}
    if not commands.allowed(user, cfg["admins"]):
        return {"user": user, "cmd": None, "reply": ("🔒 没有权限", "你不在本系统的指令白名单里，请让管理员在 设置 → 企业微信 → 指令白名单 里添加你的账号。")}
    return {"user": user, "cmd": cmd or "unknown", "arg": arg}


def _reply_async(cfg: dict, info: dict) -> None:
    from backend.app.services.wecom import client, commands

    user = info["user"]

    def reply(title: str, body: str) -> None:
        client.deliver(title, body, cfg=cfg, touser=user)

    def run():
        try:
            msg = info.get("reply")
            if msg is None and info.get("cmd"):
                msg = commands.run("" if info["cmd"] == "unknown" else info["cmd"], info.get("arg", ""), reply=reply, user=user)
            if msg:
                reply(*msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("企业微信指令回复失败：%s", exc)
            try:
                reply("❌ 出错了", str(exc)[:300])
            except Exception:  # noqa: BLE001
                pass

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


# ---------- 在线更新 ----------
@router.get("/update/check")
def update_check(force: bool = False) -> dict:
    from backend.app.services import update_service

    return update_service.check(force=force)


@router.get("/update/status")
def update_status() -> dict:
    from backend.app.services import update_service

    st = update_service.status()
    st.pop("program", None)
    return st


@router.post("/update/download")
def update_download() -> dict:
    from backend.app.services import update_service

    try:
        st = update_service.start_download()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    st.pop("program", None)
    return st


@router.post("/update/apply")
def update_apply(body: dict | None = None) -> dict:
    from backend.app.services import update_service

    if update_service.mode() == "docker" and (body or {}).get("confirm") is not True:
        raise HTTPException(status_code=400, detail="需要二次确认（confirm: true）")
    try:
        st = update_service.apply()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    st.pop("program", None)
    return st


@router.get("/backups")
def backups_list() -> dict:
    from backend.app.services import backups

    return {"dir": str(backups.backups_dir()), "items": [p.name for p in reversed(backups.list_backups())], "keep": backups.KEEP}


# ---------- v0.5.2 配置导出 / 导入 ----------
@router.get("/config/export")
def config_export(secrets: bool = False, watchlist: bool = False):
    import json as _json

    from fastapi.responses import Response as _Resp

    from backend.app.services import config_transfer
    from backend.app.services.export_service import content_disposition

    doc = config_transfer.export(include_secrets=secrets, include_watchlist=watchlist)
    return _Resp(_json.dumps(doc, ensure_ascii=False, indent=2), media_type="application/json; charset=utf-8",
                 headers={"Content-Disposition": content_disposition(config_transfer.filename(secrets)), "Cache-Control": "no-store"})


@router.post("/config/import/preview")
def config_import_preview(body: dict) -> dict:
    from backend.app.services import config_transfer

    try:
        return config_transfer.preview((body or {}).get("file"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/config/import")
def config_import(body: dict) -> dict:
    from backend.app.services import config_transfer

    body = body or {}
    try:
        return config_transfer.apply(body.get("file"), watchlist=body.get("watchlist", True) is not False, account=body.get("account") is True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------- v0.5.2 多个 AI 模型 ----------
def _profiles_call(fn, *args):
    try:
        return fn(*args)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/ai/profiles")
def ai_profiles_list() -> dict:
    from backend.app.core import ai_profiles

    return ai_profiles.public()


@router.post("/ai/profiles")
def ai_profiles_add(body: dict) -> dict:
    from backend.app.core import ai_profiles

    return _profiles_call(ai_profiles.upsert, body or {})


@router.put("/ai/profiles/{pid}")
def ai_profiles_edit(pid: str, body: dict) -> dict:
    from backend.app.core import ai_profiles

    return _profiles_call(ai_profiles.upsert, body or {}, pid)


@router.delete("/ai/profiles/{pid}")
def ai_profiles_delete(pid: str) -> dict:
    from backend.app.core import ai_profiles

    return _profiles_call(ai_profiles.delete, pid)


@router.post("/ai/profiles/{pid}/activate")
def ai_profiles_activate(pid: str) -> dict:
    from backend.app.core import ai_profiles

    return _profiles_call(ai_profiles.activate, pid)


@router.post("/ai/profiles/test")
def ai_profiles_test(body: dict | None = None) -> dict:
    """测试某个配置（body.id，可叠加未保存的 base_url / model / api_key）；不带 id 测表单里的新配置。"""
    from backend.app.core import ai_profiles
    from backend.app.core.ai_client import test_ai

    body = dict(body or {})
    settings = _profiles_call(ai_profiles.settings_for, body.pop("id", None) or "", body)
    return test_ai(settings)


# ---------- v0.5.4 手机端：模型列表 + 一键切换 ----------
@router.get("/ai/models")
def ai_models() -> dict:
    """精简的模型列表（手机切换列表用）：{active, items:[{index,id,name,model,api_key_set,active}]}。"""
    from backend.app.core import ai_profiles

    pub = ai_profiles.public()
    return {"active": pub["active"], "active_name": pub["active_name"],
            "items": [{"index": n, "id": p["id"], "name": p["name"], "model": p["model"], "api_key_set": p["api_key_set"], "active": p["active"]}
                      for n, p in enumerate(pub["items"], 1)]}


@router.post("/ai/models/active")
def ai_models_switch(body: dict) -> dict:
    """切换当前模型：body {id} 或 {index}（从 1 开始）。"""
    from backend.app.core import ai_profiles

    body = body or {}
    pid = str(body.get("id") or "")
    if not pid and body.get("index") is not None:
        items = ai_profiles.public()["items"]
        try:
            pid = items[int(body["index"]) - 1]["id"] if int(body["index"]) >= 1 else ""
        except (IndexError, ValueError, TypeError):
            pid = ""
        if not pid:
            raise HTTPException(status_code=422, detail=f"序号要在 1~{len(items)} 之间")
    if not pid:
        raise HTTPException(status_code=422, detail="需要 id 或 index")
    _profiles_call(ai_profiles.activate, pid)
    return ai_models()
