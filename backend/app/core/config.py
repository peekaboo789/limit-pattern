"""运行配置。数据目录只认 LP_DATA_DIR，否则用用户目录。"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
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


_DATA_DIR_LOCK = threading.Lock()
_DATA_DIR_CACHE: tuple[str, str, Path] | None = None


def data_dir() -> Path:
    """行情库、设置和每日总结都放在这里。

    一轮盘中扫描要给四千多只股票判板块，每只都要问一次设置，设置又落在这个目录里。
    每次问都重建 Path、stat 一遍再 mkdir 的话，判定本身就把两秒的节奏占满了，
    所以这里按环境变量缓存路径。目录被外部删掉时下面那次 `is_dir()` 会把它补回来。
    """
    global _DATA_DIR_CACHE
    raw = os.environ.get("LP_DATA_DIR", "").strip()
    env_base = os.environ.get("LOCALAPPDATA", "") if os.name == "nt" else ""
    with _DATA_DIR_LOCK:
        cached = _DATA_DIR_CACHE
    if cached is not None and cached[0] == raw and cached[1] == env_base:
        path = cached[2]
        if path.is_dir():
            return path
    if raw:
        path = Path(raw).expanduser()
    elif os.name == "nt":
        base = env_base or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "LimitPattern"
    else:
        path = Path.home() / ".limit_pattern"
    path.mkdir(parents=True, exist_ok=True)
    with _DATA_DIR_LOCK:
        _DATA_DIR_CACHE = (raw, env_base, path)
    return path


def summaries_dir() -> Path:
    path = data_dir() / "summaries"
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


_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE: tuple[tuple[str, ...], "Settings"] | None = None
_SETTINGS_ENV = ("REDIS_URL", "CACHE_ENABLED", "SUPPORTED_EXCHANGES", "DATA_PROVIDER")


def get_settings() -> Settings:
    """`Settings` 只读那四个环境变量，值没变就用同一份。

    判一只股票在不在纳入范围要问它一次，一轮全市场就是四千多次；
    pydantic-settings 每次构造都要把整个环境扫一遍。
    """
    global _SETTINGS_CACHE
    key = tuple(os.environ.get(name, "") for name in _SETTINGS_ENV)
    with _SETTINGS_LOCK:
        cached = _SETTINGS_CACHE
    if cached is not None and cached[0] == key:
        return cached[1]
    settings = Settings()
    with _SETTINGS_LOCK:
        _SETTINGS_CACHE = (key, settings)
    return settings


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
        #: 质量闸门与出数来源账。`read_settings_file()` 只回这里列出的键，
        #: 所以 `stamp()` 想留的键必须先在这里登记，否则盖下去就读不回来。
        "last_quality_at": None,
        "last_quality_errors": None,
        "last_quality_warnings": None,
        "last_sync_sources": None,
    }


_SETTINGS_FILE_LOCK = threading.Lock()
_SETTINGS_FILE_CACHE: dict | None = None
_SETTINGS_FILE_PATH = ""
_SETTINGS_FILE_SIGNATURE: tuple[int, int] | None = None
_SETTINGS_FILE_READ_AT = 0.0
#: 同进程改设置会立刻失效，这里只是给外部编辑器留一个上限：0.25 秒内不重复 stat。
SETTINGS_FILE_TTL_SECONDS = 0.25


def _settings_file_signature(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def read_settings_file() -> dict:
    """读 settings.json，按「哪份文件 + mtime + 大小」缓存，返回浅拷贝。

    一轮盘中扫描要给四千多只股票判板块归属，每只都要问一次排除哪些板块；
    每次都读盘解析的话，光这件事就把两秒的节奏占满。
    缓存必须连着文件路径一起比：换数据目录（测试用临时目录、同时跑两份程序）
    要立刻读到那一份自己的设置，不能沿用上一份。
    调用方（`update_settings`、`stamp`）拿到的是拷贝，改它不会污染缓存。
    """
    global _SETTINGS_FILE_CACHE, _SETTINGS_FILE_PATH, _SETTINGS_FILE_SIGNATURE, _SETTINGS_FILE_READ_AT
    path = settings_path()
    text_path = str(path)
    now = time.monotonic()
    with _SETTINGS_FILE_LOCK:
        if (
            _SETTINGS_FILE_CACHE is not None
            and _SETTINGS_FILE_PATH == text_path
            and now - _SETTINGS_FILE_READ_AT < SETTINGS_FILE_TTL_SECONDS
        ):
            return dict(_SETTINGS_FILE_CACHE)
    signature = _settings_file_signature(path)
    with _SETTINGS_FILE_LOCK:
        if (
            _SETTINGS_FILE_CACHE is not None
            and _SETTINGS_FILE_PATH == text_path
            and _SETTINGS_FILE_SIGNATURE == signature
        ):
            _SETTINGS_FILE_READ_AT = now
            return dict(_SETTINGS_FILE_CACHE)
    merged = default_settings()
    if signature is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            for key in merged:
                if key in payload and payload[key] is not None:
                    merged[key] = payload[key]
    with _SETTINGS_FILE_LOCK:
        _SETTINGS_FILE_CACHE = merged
        _SETTINGS_FILE_PATH = text_path
        _SETTINGS_FILE_SIGNATURE = signature
        _SETTINGS_FILE_READ_AT = time.monotonic()
    return dict(merged)


def write_settings_file(payload: dict) -> None:
    global _SETTINGS_FILE_CACHE, _SETTINGS_FILE_PATH, _SETTINGS_FILE_SIGNATURE
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    tmp.replace(path)
    with _SETTINGS_FILE_LOCK:
        _SETTINGS_FILE_CACHE = None
        _SETTINGS_FILE_PATH = ""
        _SETTINGS_FILE_SIGNATURE = None


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


def stamp(field: str, value: object) -> None:
    """往 settings.json 盖一个键。值原样写：字符串、数字、dict 都行。

    一轮同步结束要留的那份「出数来源账」是 dict，为了一个键先把整个 dict 转成字符串，
    读的那头还得再 parse 一次，两处都得记着这个约定。这里直接存结构。
    """
    current = read_settings_file()
    current[field] = value
    write_settings_file(current)


def retention_days() -> int:
    try:
        days = int(read_settings_file()["retention_trading_days"])
    except (TypeError, ValueError, KeyError):
        return DETAIL_RETENTION_TRADING_DAYS
    return max(RETENTION_MIN_TRADING_DAYS, min(RETENTION_MAX_TRADING_DAYS, days))
