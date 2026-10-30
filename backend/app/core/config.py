"""运行配置。数据目录只认 LP_DATA_DIR，否则用用户目录。"""

from __future__ import annotations

import json
import threading
import time
import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.core.constants import (
    COMPARE_WINDOW_TRADING_DAYS,
    DEFAULT_SAMPLE_POOL_TRADING_DAYS,
    POOL_HISTORY_BUFFER,
    SAMPLE_POOL_MAX_TRADING_DAYS,
    SAMPLE_POOL_MIN_TRADING_DAYS,
    DETAIL_RETENTION_TRADING_DAYS,
    RETENTION_MAX_TRADING_DAYS,
    RETENTION_MIN_TRADING_DAYS,
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    """行情库、设置和每日总结都放在这里。"""
    raw = os.environ.get("LP_DATA_DIR", "").strip()
    if raw:
        path = Path(raw).expanduser()
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "LimitPattern"
    else:
        path = Path.home() / ".limit_pattern"
    path.mkdir(parents=True, exist_ok=True)
    return path


def summaries_dir() -> Path:
    path = data_dir() / "summaries"
    path.mkdir(parents=True, exist_ok=True)
    return path


def rankings_dir() -> Path:
    path = data_dir() / "rankings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir() -> Path:
    path = data_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def static_dir() -> Path | None:
    raw = os.environ.get("LP_STATIC_DIR", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_dir() else None
    bundled = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if bundled.is_dir():
        return bundled
    return None


class Settings(BaseSettings):
    """给还在用环境变量的旧模块留的壳。业务设置以 settings.json 为准。"""

    model_config = SettingsConfigDict(extra="ignore")

    redis_url: str = ""
    cache_enabled: bool = False
    supported_exchanges: str = "SSE,SZSE"
    data_provider: str = "eastmoney"

    def resolved_database_url(self) -> str:
        override = os.environ.get("DATABASE_URL", "").strip()
        if override:
            return override
        return "sqlite:///" + (data_dir() / "lp.db").as_posix()

    def exchange_set(self) -> set[str]:
        return {part.strip().upper() for part in self.supported_exchanges.split(",") if part.strip()}

    def redis_explicit(self) -> bool:
        return bool(os.environ.get("REDIS_URL", "").strip() or self.redis_url.strip())


def get_settings() -> Settings:
    return Settings()


def scrub_secrets(text: str) -> str:
    secret = os.environ.get("AI_API_KEY", "")
    if secret and len(secret) >= 6 and secret in text:
        return text.replace(secret, "***")
    stored = read_settings_file().get("ai_api_key") or ""
    if stored and len(stored) >= 6 and stored in text:
        return text.replace(stored, "***")
    return text


def settings_path() -> Path:
    return data_dir() / "settings.json"


def default_settings() -> dict:
    return {
        "retention_trading_days": DETAIL_RETENTION_TRADING_DAYS,
        "sample_pool_trading_days": DEFAULT_SAMPLE_POOL_TRADING_DAYS,
        "settings_schema": SETTINGS_SCHEMA,
        "ai_base_url": os.environ.get("AI_BASE_URL", "").strip(),
        "ai_model": os.environ.get("AI_MODEL", "").strip(),
        "ai_api_key": os.environ.get("AI_API_KEY", "").strip(),
        "data_source": os.environ.get("DATA_PROVIDER", "auto").strip() or "auto",
        "active_source": "",
        "ai_thinking": "auto",
        "ai_thinking_learned": {},
        "excluded_boards": ["star"],
        "last_sync_at": None,
        "last_cleanup_at": None,
    }


_SETTINGS_LOCK = threading.RLock()
_SETTINGS_CACHE: dict = {"path": None, "mtime": None, "payload": None}


def _load_settings_payload(path: Path):
    """Windows 上写文件和读文件撞在一起会 PermissionError，重试几次；仍失败就用上一次读到的内容，
    不能悄悄退回默认值（那会让数据源看起来「切换不生效」）。"""
    with _SETTINGS_LOCK:
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        cache = _SETTINGS_CACHE
        if cache["path"] == str(path) and cache["mtime"] == mtime and cache["payload"] is not None:
            return cache["payload"]
        last_error = None
        for attempt in range(5):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                cache.update(path=str(path), mtime=mtime, payload=payload)
                return payload
            except (OSError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        if cache["path"] == str(path) and cache["payload"] is not None:
            return cache["payload"]
        _ = last_error
        return None


SETTINGS_SCHEMA = 2


def migrate_settings() -> bool:
    """旧版（无 settings_schema 或 <2）的设置文件：样本池强制 30、保留 40，覆盖旧的 3/5 默认值。
    schema 已是最新时尊重用户后来自己改的值。返回是否做了迁移。"""
    path = settings_path()
    if not path.exists():
        return False
    with _SETTINGS_LOCK:
        payload = _load_settings_payload(path)
        if not isinstance(payload, dict):
            return False
        try:
            schema = int(payload.get("settings_schema") or 0)
        except (TypeError, ValueError):
            schema = 0
        if schema >= SETTINGS_SCHEMA:
            return False
        current = dict(payload)
        for old in ("compare_window_trading_days", "pool_days", "sample_pool_days"):
            current.pop(old, None)
        current["sample_pool_trading_days"] = max(DEFAULT_SAMPLE_POOL_TRADING_DAYS, _clamp_pool(payload.get("sample_pool_trading_days") or 0))
        try:
            old_retention = int(payload.get("retention_trading_days") or 0)
        except (TypeError, ValueError):
            old_retention = 0
        current["retention_trading_days"] = max(DETAIL_RETENTION_TRADING_DAYS, old_retention, current["sample_pool_trading_days"] + POOL_HISTORY_BUFFER)
        current["settings_schema"] = SETTINGS_SCHEMA
        write_settings_file(current)
        return True


_MIGRATED: set[str] = set()


def read_settings_file() -> dict:
    path = settings_path()
    merged = default_settings()
    if not path.exists():
        return merged
    if str(path) not in _MIGRATED:
        _MIGRATED.add(str(path))
        try:
            migrate_settings()
        except Exception:
            pass
    payload = _load_settings_payload(path)
    if payload is None:
        return merged
    if isinstance(payload, dict):
        for key in merged:
            if key in payload and payload[key] is not None:
                merged[key] = payload[key]
    return merged


def write_settings_file(payload: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with _SETTINGS_LOCK:
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        for attempt in range(10):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        _SETTINGS_CACHE.update(path=str(path), mtime=mtime, payload=json.loads(text))


def excluded_boards() -> set[str]:
    """默认剔除科创板。环境变量 EXCLUDED_BOARDS 优先，空字符串表示一块都不剔。"""
    if "EXCLUDED_BOARDS" in os.environ:
        return {part.strip() for part in os.environ["EXCLUDED_BOARDS"].split(",") if part.strip()}
    raw = read_settings_file().get("excluded_boards")
    if isinstance(raw, str):
        return {part.strip() for part in raw.split(",") if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return {"star"}


def public_settings() -> dict:
    raw = read_settings_file()
    key = str(raw.get("ai_api_key") or "")
    url = str(raw.get("ai_base_url") or "")
    model = str(raw.get("ai_model") or "")
    return {
        "retention_trading_days": retention_days(),
        "retention_min_trading_days": min_retention_days(),
        "sample_pool_trading_days": sample_pool_days(),
        "sample_pool_range": [SAMPLE_POOL_MIN_TRADING_DAYS, SAMPLE_POOL_MAX_TRADING_DAYS],
        "feature_window_trading_days": COMPARE_WINDOW_TRADING_DAYS,
        "compare_window_trading_days": COMPARE_WINDOW_TRADING_DAYS,
        "ai_enabled": bool(url and model and key),
        "ai_base_url": url,
        "ai_model": model,
        "ai_api_key_set": bool(key),
        "ai_thinking": str(raw.get("ai_thinking") or "auto"),
        "data_source": str(raw.get("data_source") or "auto"),
        "data_source_options": ["auto", "eastmoney", "tencent", "sina"],
        "active_source": str(raw.get("active_source") or ""),
        "excluded_boards": sorted(excluded_boards()),
        "included_boards": _included_board_names(),
        "market_scope": _market_scope_text(),
        "last_sync_at": raw.get("last_sync_at"),
        "last_cleanup_at": raw.get("last_cleanup_at"),
    }


def _included_board_names() -> list[str]:
    from backend.app.providers.markets import included_board_names

    return included_board_names()


def _market_scope_text() -> str:
    from backend.app.providers.markets import market_scope_text

    return market_scope_text()


def update_settings(changes: dict) -> dict:
    with _SETTINGS_LOCK:
        return _update_settings(changes)


def _update_settings(changes: dict) -> dict:
    current = read_settings_file()
    if "compare_window_trading_days" in changes and changes["compare_window_trading_days"] not in (
        None,
        COMPARE_WINDOW_TRADING_DAYS,
    ):
        raise ValueError("对比窗口固定为 3 个交易日，不能修改")
    if "retention_trading_days" in changes and changes["retention_trading_days"] is not None:
        days = int(changes["retention_trading_days"])
        if days < RETENTION_MIN_TRADING_DAYS or days > RETENTION_MAX_TRADING_DAYS:
            raise ValueError(
                f"保留天数要在 {RETENTION_MIN_TRADING_DAYS} 到 {RETENTION_MAX_TRADING_DAYS} 之间"
            )
        current["retention_trading_days"] = days
    if "sample_pool_trading_days" in changes and changes["sample_pool_trading_days"] is not None:
        pool = int(changes["sample_pool_trading_days"])
        if pool < SAMPLE_POOL_MIN_TRADING_DAYS or pool > SAMPLE_POOL_MAX_TRADING_DAYS:
            raise ValueError(f"涨停样本池要在 {SAMPLE_POOL_MIN_TRADING_DAYS} 到 {SAMPLE_POOL_MAX_TRADING_DAYS} 个交易日之间")
        current["sample_pool_trading_days"] = pool
    floor = max(_clamp_pool(current.get("sample_pool_trading_days")), HISTORY_FLOOR_POOL) + POOL_HISTORY_BUFFER
    try:
        if int(current.get("retention_trading_days") or 0) < floor:
            current["retention_trading_days"] = floor  # 保留天数不能少于样本池 + 特征窗口 + 5 日基准
    except (TypeError, ValueError):
        current["retention_trading_days"] = max(DETAIL_RETENTION_TRADING_DAYS, floor)
    if "ai_base_url" in changes and changes["ai_base_url"] is not None:
        current["ai_base_url"] = str(changes["ai_base_url"]).strip()
    if "ai_model" in changes and changes["ai_model"] is not None:
        current["ai_model"] = str(changes["ai_model"]).strip()
    if changes.get("ai_thinking") is not None:
        mode = str(changes["ai_thinking"]).strip()
        if mode not in ("auto", "on", "off"):
            raise ValueError("深度思考只能是 auto / on / off")
        current["ai_thinking"] = mode
    if changes.get("ai_api_key"):
        current["ai_api_key"] = str(changes["ai_api_key"]).strip()
    if "data_source" in changes and changes["data_source"] is not None:
        from backend.app.providers.router import normalize_source

        current["data_source"] = normalize_source(str(changes["data_source"]))
    write_settings_file(current)
    return public_settings()


def stamp(field: str, value: str) -> None:
    with _SETTINGS_LOCK:
        current = read_settings_file()
        current[field] = value
        write_settings_file(current)


def _clamp_pool(value) -> int:
    try:
        pool = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_POOL_TRADING_DAYS
    return max(SAMPLE_POOL_MIN_TRADING_DAYS, min(SAMPLE_POOL_MAX_TRADING_DAYS, pool))


def sample_pool_days() -> int:
    """涨停样本池长度（交易日）。"""
    return _clamp_pool(read_settings_file().get("sample_pool_trading_days", DEFAULT_SAMPLE_POOL_TRADING_DAYS))


HISTORY_FLOOR_POOL = 30


def min_retention_days() -> int:
    """历史下限：max(样本池, 30) + 8（3 根特征 + 5 日量比基准）。清理和补全历史都不低于它。"""
    return max(sample_pool_days(), HISTORY_FLOOR_POOL) + POOL_HISTORY_BUFFER


def retention_days() -> int:
    """明细保留天数，永远不少于样本池 + 特征窗口 + 5 日基准。"""
    try:
        days = int(read_settings_file()["retention_trading_days"])
    except (TypeError, ValueError, KeyError):
        days = DETAIL_RETENTION_TRADING_DAYS
    days = max(RETENTION_MIN_TRADING_DAYS, min(RETENTION_MAX_TRADING_DAYS, days))
    return max(days, min_retention_days())
