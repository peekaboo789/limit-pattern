"""``LimitPattern.exe --apply-update --target <旧程序目录> --pid <旧进程>``：由下载好的新版程序执行。

等旧进程退出 → 用自己所在目录替换旧程序目录（失败自动还原）→ 启动新版 → 退出。不碰数据目录。"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("limit_pattern.updater")


def wait_pid(pid: int, timeout: float = 60.0) -> bool:
    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return True
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    import os

    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.3)
    return False


def apply_update(target: Path, source: Path, pid: int, launch: bool = True) -> int:
    from backend.app.services.update_service import swap_install

    if not wait_pid(pid):
        logger.error("旧程序 60 秒没退出，放弃更新")
        return 2
    time.sleep(1.0)  # 等文件句柄释放
    for attempt in range(5):
        try:
            swap_install(target, source)
            break
        except PermissionError as exc:  # 杀毒软件 / 资源管理器偶尔还占着文件
            logger.warning("替换被占用（%s），重试 %d", exc, attempt + 1)
            time.sleep(2.0)
        except Exception:
            logger.exception("替换失败，已还原旧版本")
            if launch:
                subprocess.Popen([str(target / "LimitPattern.exe")], cwd=str(target))
            return 1
    else:
        if launch:
            subprocess.Popen([str(target / "LimitPattern.exe")], cwd=str(target))
        return 1
    logger.info("更新完成：%s", target)
    if launch:
        subprocess.Popen([str(target / "LimitPattern.exe")], cwd=str(target))
    return 0
