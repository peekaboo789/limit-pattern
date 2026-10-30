"""运行配置。数据目录只认 LP_DATA_DIR，否则用用户目录。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.core.constants import (
    COMPARE_WINDOW_TRADING_DAYS,
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
        "ai_base_url": os.environ.get("AI_BASE_URL", "").strip(),
        "ai_model": os.environ.get("AI_MODEL", "").strip(),
        "ai_api_key": os.environ.get("AI_API_KEY", "").strip(),
        "data_source": os.environ.get("DATA_PROVIDER", "auto").strip() or "auto",
        "active_source": "",
        "excluded_boards": ["star"],
        "last_sync_at": None,
        "last_cleanup_at": None,
    }


def read_settings_file() -> dict:
    path = settings_path()
    merged = default_settings()
    if not path.exists():
        return merged
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    tmp.replace(path)


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
        "retention_trading_days": int(raw["retention_trading_days"]),
        "compare_window_trading_days": COMPARE_WINDOW_TRADING_DAYS,
        "ai_enabled": bool(url and model and key),
        "ai_base_url": url,
        "ai_model": model,
        "ai_api_key_set": bool(key),
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
    if "ai_base_url" in changes and changes["ai_base_url"] is not None:
        current["ai_base_url"] = str(changes["ai_base_url"]).strip()
    if "ai_model" in changes and changes["ai_model"] is not None:
        current["ai_model"] = str(changes["ai_model"]).strip()
    if changes.get("ai_api_key"):
        current["ai_api_key"] = str(changes["ai_api_key"]).strip()
    if "data_source" in changes and changes["data_source"] is not None:
        from backend.app.providers.router import normalize_source

        current["data_source"] = normalize_source(str(changes["data_source"]))
    write_settings_file(current)
    return public_settings()


def stamp(field: str, value: str) -> None:
    current = read_settings_file()
    current[field] = value
    write_settings_file(current)


def retention_days() -> int:
    try:
        days = int(read_settings_file()["retention_trading_days"])
    except (TypeError, ValueError, KeyError):
        return DETAIL_RETENTION_TRADING_DAYS
    return max(RETENTION_MIN_TRADING_DAYS, min(RETENTION_MAX_TRADING_DAYS, days))
