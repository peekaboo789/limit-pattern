"""v0.5.2 设置配置导出 / 导入。

文件格式（version 1）：
{"format": "limit-pattern-config", "version": 1, "app_version", "exported_at", "includes_secrets",
 "settings": {...}, "watchlist": [...]?, "account": {"username", "hash"}?}
默认不含密钥（AI Key、企业微信 Secret/Token/EncodingAESKey/webhook、登录密码哈希）；勾选「包含密钥」才写进去。
导入：先预览差异，再应用；应用前备份当前设置；文件里的密钥为空时保留现有的。"""

from __future__ import annotations

import json
from datetime import datetime

from backend.app import __version__
from backend.app.core.config import (
    _SETTINGS_LOCK,
    WECOM_SECRET_FIELDS,
    default_settings,
    public_settings,
    read_settings_file,
    write_settings_file,
)

FORMAT = "limit-pattern-config"
VERSION = 1
SECRET_KEYS = ("ai_api_key", "access_password", *WECOM_SECRET_FIELDS)
SKIP_KEYS = ("last_sync_at", "last_cleanup_at", "settings_schema", "ai_thinking_learned", "active_source")
LABELS = {
    "data_source": "数据源", "retention_trading_days": "保留交易日", "sample_pool_trading_days": "涨停样本池",
    "ai_base_url": "AI 接口地址", "ai_model": "AI 模型", "ai_api_key": "AI API Key", "ai_profiles": "AI 模型列表",
    "ai_active_profile": "当前 AI 模型", "ai_thinking": "深度思考", "ai_timeout_seconds": "AI 超时", "watchlist_alerts": "自选股报警",
    "alert_trading_hours_only": "只在交易时段报警", "excluded_boards": "排除板块", "wecom_enabled": "企业微信开关",
    "wecom_corpid": "企业ID", "wecom_agentid": "AgentId", "wecom_secret": "企业微信 Secret", "wecom_token": "回调 Token",
    "wecom_aes_key": "EncodingAESKey", "wecom_webhook": "群机器人 webhook", "wecom_events": "推送事件", "wecom_msg_format": "消息格式", "wecom_touser": "企业微信接收人", "wecom_toparty": "接收部门", "wecom_totag": "接收标签",
    "wecom_proxy": "企业微信接口地址", "wecom_admins": "指令白名单", "update_mirror": "更新镜像",
}
MASK = "（密钥）"


def export(include_secrets: bool = False, include_watchlist: bool = False) -> dict:
    raw = read_settings_file()
    from backend.app.core.ai_profiles import ensure

    ensure(raw)
    out: dict = {}
    for key, value in raw.items():
        if key in SKIP_KEYS:
            continue
        if key in SECRET_KEYS and not include_secrets:
            continue
        if key == "access_password":
            continue  # 已废弃（v0.5.1 起账号在 auth.json）
        out[key] = value
    profiles = []
    for p in raw.get("ai_profiles") or []:
        p = dict(p)
        if not include_secrets:
            p.pop("api_key", None)
        profiles.append(p)
    out["ai_profiles"] = profiles
    doc = {"format": FORMAT, "version": VERSION, "app_version": __version__,
           "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "includes_secrets": include_secrets, "settings": out}
    if include_watchlist:
        from backend.app.services import watchlist_service as ws

        doc["watchlist"] = [{k: x.get(k, "") for k in ("code", "name", "note")} for x in ws.load()]
    if include_secrets:
        from backend.app.core import auth

        acc = auth.account()
        if acc and not acc["from_env"]:
            doc["account"] = {"username": acc["username"], "hash": acc["hash"]}
    return doc


def filename(include_secrets: bool) -> str:
    return f"涨停形态系统_配置_{'含密钥_' if include_secrets else ''}{datetime.now().strftime('%Y%m%d_%H%M')}.json"


def parse(doc) -> dict:
    if isinstance(doc, (str, bytes)):
        try:
            doc = json.loads(doc)
        except ValueError as exc:
            raise ValueError("不是合法的 JSON 文件") from exc
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("这不是本系统导出的配置文件")
    try:
        version = int(doc.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1:
        raise ValueError("配置文件版本不对")
    if version > VERSION:
        raise ValueError(f"配置文件来自更新的版本（格式 v{version}），请先升级本程序")
    if not isinstance(doc.get("settings"), dict):
        raise ValueError("配置文件里没有 settings")
    return doc


def _merged(doc: dict) -> dict:
    """按导入文件算出新的 settings（不写盘）。只认识当前版本有的键；类型不对的跳过。"""
    current = read_settings_file()
    from backend.app.core.ai_profiles import ensure

    ensure(current)
    defaults = default_settings()
    new = dict(current)
    for key, value in doc["settings"].items():
        if key not in defaults or key in SKIP_KEYS or key == "access_password":
            continue
        if key == "ai_profiles":
            continue
        if key in SECRET_KEYS:
            if value:  # 密钥只在文件里有值时覆盖
                new[key] = str(value)
            continue
        default = defaults[key]
        if default is not None and value is not None and not isinstance(value, type(default)) and not (
                isinstance(default, (int, float)) and isinstance(value, (int, float)) and not isinstance(value, bool)):
            continue
        new[key] = value
    if isinstance(doc["settings"].get("ai_profiles"), list):
        old = {p["id"]: p for p in current.get("ai_profiles") or [] if isinstance(p, dict) and p.get("id")}
        profiles = []
        for p in doc["settings"]["ai_profiles"][:20]:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            item = {k: p.get(k) for k in ("id", "name", "base_url", "model", "temperature", "max_tokens")}
            item["api_key"] = p.get("api_key") or (old.get(p["id"]) or {}).get("api_key", "")
            profiles.append(item)
        # 现有但文件里没有的配置保留（导入只增改不删）
        ids = {p["id"] for p in profiles}
        profiles += [p for pid, p in old.items() if pid not in ids]
        new["ai_profiles"] = profiles
        if new.get("ai_active_profile") not in {p["id"] for p in profiles}:
            new["ai_active_profile"] = profiles[0]["id"] if profiles else None
        from backend.app.core.ai_profiles import _mirror

        _mirror(new)
    return new


def _show(key: str, value) -> str:
    if key in SECRET_KEYS:
        return MASK if value else "（空）"
    if key == "ai_profiles":
        return "、".join(f"{p.get('name')}（{p.get('model')}）" for p in value or []) or "（空）"
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= 120 else text[:117] + "…"


def preview(doc) -> dict:
    doc = parse(doc)
    current = read_settings_file()
    from backend.app.core.ai_profiles import ensure

    ensure(current)
    new = _merged(doc)
    changes = []
    for key in sorted(set(new) | set(current)):
        if key in SKIP_KEYS or key in ("access_password",):
            continue
        if new.get(key) != current.get(key):
            changes.append({"key": key, "label": LABELS.get(key, key), "old": _show(key, current.get(key)), "new": _show(key, new.get(key)),
                            "secret": key in SECRET_KEYS})
    out = {"app_version": doc.get("app_version"), "exported_at": doc.get("exported_at"), "includes_secrets": bool(doc.get("includes_secrets")),
           "changes": changes, "watchlist_count": len(doc["watchlist"]) if isinstance(doc.get("watchlist"), list) else None,
           "has_account": bool(doc.get("account"))}
    return out


def apply(doc, *, watchlist: bool = True, account: bool = False) -> dict:
    doc = parse(doc)
    from backend.app.services import backups

    backup = backups.make_backup("pre-config-import")
    with _SETTINGS_LOCK:
        write_settings_file(_merged(doc))
    applied_wl = None
    if watchlist and isinstance(doc.get("watchlist"), list):
        from backend.app.services import watchlist_service as ws

        entries = [x for x in doc["watchlist"] if isinstance(x, dict) and x.get("code")]
        ws.remove(clear=True)
        applied_wl = len(ws.add(entries)["added"])
    applied_account = False
    acc = doc.get("account")
    if account and isinstance(acc, dict) and str(acc.get("hash", "")).startswith("pbkdf2$") and acc.get("username"):
        from backend.app.core import auth

        if not auth.env_account():
            with auth._LOCK:
                data = auth._load()
                data.update(username=str(acc["username"])[:32], hash=acc["hash"], sessions=[])
                auth._save(data)
            applied_account = True
    return {"ok": True, "backup": backup.name, "watchlist": applied_wl, "account": applied_account, "settings": public_settings()}
