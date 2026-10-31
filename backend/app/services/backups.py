"""升级前后的数据保护：新版本第一次启动时自动备份设置和数据库到 data/backups/（只保留最近 3 份），再跑迁移。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from backend.app import __version__
from backend.app.core.config import data_dir
from backend.app.core.logging import get_logger

logger = get_logger("limit_pattern.backup")
KEEP = 3
MARKER = ".app_version"
FILES = ("settings.json", "watchlist.json", "wecom_sent.json", "auth.json", ".session_secret")


def backups_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_backup(label: str) -> Path:
    root = data_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in label)[:60]
    dest = backups_dir() / f"{stamp}_{safe}"
    n = 1
    while dest.exists():
        n += 1
        dest = backups_dir() / f"{stamp}_{safe}_{n}"
    dest.mkdir(parents=True)
    for name in FILES:
        src = root / name
        if src.is_file():
            shutil.copy2(src, dest / name)
    db = root / "lp.db"
    if db.is_file():
        # 用 SQLite 在线备份，WAL 里没落盘的也一起拷，服务运行中也安全
        with sqlite3.connect(str(db)) as src, sqlite3.connect(str(dest / "lp.db")) as out:
            src.backup(out)
    (dest / "info.json").write_text(json.dumps({"label": label, "created_at": stamp, "app_version": __version__}, ensure_ascii=False), encoding="utf-8")
    prune()
    logger.info("已备份数据到 %s", dest)
    return dest


def list_backups() -> list[Path]:
    return sorted([p for p in backups_dir().iterdir() if p.is_dir()], key=lambda p: p.name)


def prune(keep: int = KEEP) -> None:
    items = list_backups()
    for old in items[:-keep] if len(items) > keep else []:
        shutil.rmtree(old, ignore_errors=True)


def previous_version() -> str | None:
    try:
        return (data_dir() / MARKER).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def migrate_v051() -> bool:
    """0.5.0 → 0.5.1：企业微信消息格式默认文本（markdown 在个人微信里显示「暂不支持此消息类型」）。"""
    from backend.app.core.config import _SETTINGS_LOCK, _load_settings_payload, settings_path, write_settings_file

    path = settings_path()
    if not path.exists():
        return False
    with _SETTINGS_LOCK:
        payload = _load_settings_payload(path)
        if not isinstance(payload, dict) or payload.get("wecom_msg_format") in ("text", "textcard", "markdown"):
            return False
        payload["wecom_msg_format"] = "text"
        write_settings_file(payload)
    return True


def on_start() -> dict:
    """返回 {previous, current, changed, backup}。数据目录是空的新装不备份。"""
    root = data_dir()
    prev = previous_version()
    has_data = prev is not None or (root / "settings.json").exists()  # 全新安装不备份
    result = {"previous": prev, "current": __version__, "changed": prev != __version__, "backup": None}
    if result["changed"] and has_data:
        try:
            result["backup"] = str(make_backup(f"v{prev or 'old'}-to-v{__version__}"))
        except Exception:  # noqa: BLE001 - 备份失败不挡启动，但要记日志
            logger.exception("升级备份失败")
    migrate_v051()
    if result["changed"]:
        (root / MARKER).write_text(__version__, encoding="utf-8")
    return result
