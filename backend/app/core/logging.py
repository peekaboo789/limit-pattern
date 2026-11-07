"""日志只走标准输出。桌面版没有控制台，才显式改到 data/app.log。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False
LOGGER_NAME = "limit_pattern"


def configure_logging(prefer_file: bool = False, log_path: Path | None = None) -> logging.Logger:
    """配置根日志器。prefer_file 为真时改挂文件，不再偷偷留在没人看的 stdout 上。"""
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if prefer_file:
        if log_path is None:
            from backend.app.core.config import data_dir

            log_path = data_dir() / "app.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(log_path, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging(prefer_file=False)
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
