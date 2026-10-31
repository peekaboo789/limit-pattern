"""v0.5.2 多个 AI 模型配置，一键切换。

settings.json：ai_profiles = [{id, name, base_url, model, api_key, temperature, max_tokens}]，ai_active_profile = id。
当前配置同时镜像到旧的平铺字段 ai_base_url / ai_model / ai_api_key / ai_temperature / ai_max_tokens，
所以所有调用 AI 的地方（总结、榜单、测试）不用改。密钥只写不读，接口只返回 api_key_set。"""

from __future__ import annotations

import secrets

from backend.app.core.config import _SETTINGS_LOCK, read_settings_file, write_settings_file

MAX_PROFILES = 20
FLAT = {"base_url": "ai_base_url", "model": "ai_model", "api_key": "ai_api_key", "temperature": "ai_temperature", "max_tokens": "ai_max_tokens"}


def _new_id() -> str:
    return "m" + secrets.token_hex(4)


def ensure(raw: dict) -> bool:
    """把旧的单个 AI 配置迁移成第一个配置（只做一次）。返回是否改动。"""
    profiles = raw.get("ai_profiles")
    if isinstance(profiles, list) and profiles:
        return False
    if not (raw.get("ai_base_url") or raw.get("ai_model") or raw.get("ai_api_key")):
        return False
    pid = _new_id()
    raw["ai_profiles"] = [{"id": pid, "name": raw.get("ai_model") or "默认模型", "base_url": raw.get("ai_base_url") or "",
                           "model": raw.get("ai_model") or "", "api_key": raw.get("ai_api_key") or "",
                           "temperature": raw.get("ai_temperature"), "max_tokens": raw.get("ai_max_tokens")}]
    raw["ai_active_profile"] = pid
    return True


def _load() -> dict:
    raw = read_settings_file()
    if ensure(raw):
        write_settings_file(raw)
    return raw


def public(raw: dict | None = None) -> dict:
    raw = dict(raw if raw is not None else _load())
    ensure(raw)
    items = [{"id": p["id"], "name": p.get("name", ""), "base_url": p.get("base_url", ""), "model": p.get("model", ""),
              "api_key_set": bool(p.get("api_key")), "temperature": p.get("temperature"), "max_tokens": p.get("max_tokens"),
              "active": p["id"] == raw.get("ai_active_profile")} for p in raw.get("ai_profiles") or []]
    active = next((p for p in items if p["active"]), None)
    return {"items": items, "active": active["id"] if active else None, "active_name": active["name"] if active else None}


def _clean(body: dict, old: dict | None) -> dict:
    p = dict(old or {"id": _new_id()})
    for k in ("name", "base_url", "model"):
        if body.get(k) is not None:
            p[k] = str(body[k]).strip()[:300]
    if not p.get("name"):
        p["name"] = p.get("model") or "未命名模型"
    if p.get("base_url") and not str(p["base_url"]).startswith(("http://", "https://")):
        raise ValueError("接口地址要以 http:// 或 https:// 开头")
    if body.get("api_key"):
        p["api_key"] = str(body["api_key"]).strip()
    if body.get("api_key_clear") is True:
        p["api_key"] = ""
    if "temperature" in body:
        t = body["temperature"]
        if t in (None, ""):
            p["temperature"] = None
        else:
            try:
                t = float(t)
            except (TypeError, ValueError) as exc:
                raise ValueError("temperature 要是 0–2 的数字") from exc
            if not 0 <= t <= 2:
                raise ValueError("temperature 要在 0–2 之间")
            p["temperature"] = t
    if "max_tokens" in body:
        m = body["max_tokens"]
        if m in (None, ""):
            p["max_tokens"] = None
        else:
            try:
                m = int(m)
            except (TypeError, ValueError) as exc:
                raise ValueError("max_tokens 要是整数") from exc
            if not 16 <= m <= 200000:
                raise ValueError("max_tokens 要在 16–200000 之间")
            p["max_tokens"] = m
    p.setdefault("api_key", "")
    p.setdefault("temperature", None)
    p.setdefault("max_tokens", None)
    return p


def _mirror(raw: dict) -> None:
    active = next((p for p in raw.get("ai_profiles") or [] if p["id"] == raw.get("ai_active_profile")), None)
    for k, flat in FLAT.items():
        raw[flat] = (active or {}).get(k) if active else ("" if k in ("base_url", "model", "api_key") else None)
        if raw[flat] is None and k in ("base_url", "model", "api_key"):
            raw[flat] = ""


def upsert(body: dict, pid: str | None = None) -> dict:
    with _SETTINGS_LOCK:
        raw = _load()
        profiles = list(raw.get("ai_profiles") or [])
        if pid:
            idx = next((i for i, p in enumerate(profiles) if p["id"] == pid), None)
            if idx is None:
                raise LookupError("没有这个模型配置")
            profiles[idx] = _clean(body, profiles[idx])
        else:
            if len(profiles) >= MAX_PROFILES:
                raise ValueError(f"最多 {MAX_PROFILES} 个模型配置")
            profiles.append(_clean(body, None))
            if not raw.get("ai_active_profile") or body.get("activate"):
                raw["ai_active_profile"] = profiles[-1]["id"]
        raw["ai_profiles"] = profiles
        _mirror(raw)
        write_settings_file(raw)
    return public()


def delete(pid: str) -> dict:
    with _SETTINGS_LOCK:
        raw = _load()
        profiles = [p for p in raw.get("ai_profiles") or [] if p["id"] != pid]
        if len(profiles) == len(raw.get("ai_profiles") or []):
            raise LookupError("没有这个模型配置")
        raw["ai_profiles"] = profiles
        if raw.get("ai_active_profile") == pid:
            raw["ai_active_profile"] = profiles[0]["id"] if profiles else None
        _mirror(raw)
        write_settings_file(raw)
    return public()


def activate(pid: str) -> dict:
    with _SETTINGS_LOCK:
        raw = _load()
        if not any(p["id"] == pid for p in raw.get("ai_profiles") or []):
            raise LookupError("没有这个模型配置")
        raw["ai_active_profile"] = pid
        _mirror(raw)
        write_settings_file(raw)
    return public()


def settings_for(pid: str, overrides: dict | None = None) -> dict:
    """测试连接用：拿某个配置（可叠加表单里未保存的值）拼成 chat() 需要的设置。"""
    raw = _load()
    prof = next((p for p in raw.get("ai_profiles") or [] if p["id"] == pid), None) if pid else None
    if pid and prof is None:
        raise LookupError("没有这个模型配置")
    merged = _clean(overrides or {}, dict(prof or {"id": "tmp"}))
    out = dict(raw)
    for k, flat in FLAT.items():
        out[flat] = merged.get(k)
    return out


def sync_from_flat(raw: dict) -> None:
    """旧设置表单直接改 ai_base_url / ai_model / ai_api_key 时，同步到当前配置。"""
    ensure(raw)
    for p in raw.get("ai_profiles") or []:
        if p["id"] == raw.get("ai_active_profile"):
            for k in ("base_url", "model", "api_key"):
                p[k] = raw.get(FLAT[k]) or ""
