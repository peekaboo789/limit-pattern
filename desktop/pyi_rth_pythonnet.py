# PyInstaller runtime hook。必须在 import clr / import webview 之前执行。
# 不设置 PYTHONNET_RUNTIME：Windows 上 pythonnet 3 的默认运行时是 .NET Framework，
# pywebview 的 WinForms 宿主也走这条路径。

from __future__ import annotations

import os
import sys


def _add(path: str, found: list[str]) -> None:
    if path and os.path.isdir(path) and path not in found:
        found.append(path)


if sys.platform == "win32" and getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    directories: list[str] = []
    _add(base, directories)
    _add(exe_dir, directories)
    _add(os.path.join(base, "pythonnet", "runtime"), directories)
    _add(os.path.join(base, "webview", "lib"), directories)
    _add(os.path.join(base, "webview", "lib", "runtimes", "win-x64", "native"), directories)
    _add(os.path.join(base, "clr_loader", "ffi", "dlls", "amd64"), directories)

    if os.path.isdir(base):
        base_depth = base.rstrip("\\/").count(os.sep)
        for root, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name not in {"__pycache__", "tests", "test"}]
            if root.count(os.sep) - base_depth > 6:
                dirnames.clear()
                continue
            if any(name in filenames for name in ("Python.Runtime.dll", "ClrLoader.dll", "WebView2Loader.dll")):
                _add(root, directories)

    dll_name = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
    for folder in directories:
        candidate = os.path.join(folder, dll_name)
        if os.path.isfile(candidate):
            os.environ["PYTHONNET_PYDLL"] = candidate
            break

    current = [item for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    os.environ["PATH"] = os.pathsep.join(directories + [item for item in current if item not in directories])

    if hasattr(os, "add_dll_directory"):
        for folder in directories:
            try:
                os.add_dll_directory(folder)
            except OSError:
                pass
