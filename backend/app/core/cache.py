"""进程内缓存。只有环境里真的写了 REDIS_URL 才去敲门。"""

from __future__ import annotations

import time
from typing import Any

from backend.app.core.config import get_settings
from backend.app.core.logging import get_logger

logger = get_logger("cache")

_MEMORY: dict[str, tuple[float, Any]] = {}
_BACKEND = ""


def cache_backend_name() -> str:
    global _BACKEND
    if _BACKEND:
        return _BACKEND
    settings = get_settings()
    if not settings.cache_enabled:
        _BACKEND = "off"
        logger.info("CACHE_ENABLED=false，缓存关闭")
        return _BACKEND
    if not settings.redis_explicit():
        _BACKEND = "memory"
        logger.info("没有配置 REDIS_URL，直接用进程内缓存")
        return _BACKEND
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(settings.redis_url, socket_timeout=0.4)
        client.ping()
        _BACKEND = "redis"
        logger.info("缓存=redis")
    except Exception:
        _BACKEND = "memory"
        logger.info("Redis 写入失败，改用进程内缓存")
    return _BACKEND


def cache_get(key: str) -> Any | None:
    if cache_backend_name() == "off":
        return None
    if cache_backend_name() == "redis":
        return _redis_get(key)
    item = _MEMORY.get(key)
    if not item:
        return None
    expires, value = item
    if expires < time.time():
        _MEMORY.pop(key, None)
        return None
    return value


def cache_set(key: str, value: Any, ttl: float = 8.0) -> None:
    if cache_backend_name() == "off":
        return
    if cache_backend_name() == "redis":
        _redis_set(key, value, ttl)
        return
    _MEMORY[key] = (time.time() + ttl, value)


def cache_clear() -> None:
    _MEMORY.clear()


def _redis_client():
    import redis

    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=0.4)


def _redis_get(key: str) -> Any | None:
    import json

    try:
        raw = _redis_client().get(key)
    except Exception:
        return None
    if raw is None:
        return None
    return json.loads(raw)


def _redis_set(key: str, value: Any, ttl: float) -> None:
    import json

    try:
        _redis_client().set(key, json.dumps(value, ensure_ascii=False), ex=max(1, int(ttl)))
    except Exception:
        logger.info("Redis 写入失败，改用进程内缓存")
        global _BACKEND
        _BACKEND = "memory"
        _MEMORY[key] = (time.time() + ttl, value)
