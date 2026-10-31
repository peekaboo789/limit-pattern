"""onedir / onefile 共用的 PyInstaller Analysis 参数。

仓库根目录在打包机上可能还没有 backend/ 或 frontend/dist（另外两个 agent 并行开发）。
存在则打进包，不存在就只用 desktop 里的 stub。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

SPEC_DIR = Path(__file__).resolve().parent
ROOT = SPEC_DIR.parent

# uvicorn 用 importlib 按字符串加载协议和事件循环，静态分析经常漏掉。
UVICORN_HIDDEN = [
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "h11",
    "httptools",
    "websockets",
    "websockets.legacy",
    "websockets.legacy.server",
    "watchfiles",
    "anyio",
    "anyio._backends._asyncio",
    "sniffio",
    "fastapi",
    "starlette",
    "starlette.routing",
    "starlette.middleware",
    "starlette.middleware.cors",
    "starlette.staticfiles",
    "pydantic",
    "pydantic.deprecated.decorator",
    "pydantic_core",
    "pydantic_core._pydantic_core",
]

WINDOWS_GUI_HIDDEN = [
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.win32",
    "clr",
    "clr_loader",
    "pythonnet",
    "cffi",
    "_cffi_backend",
    "pystray",
    "pystray._win32",
    "pystray._base",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.IcoImagePlugin",
]


def _log(message: str) -> None:
    """Windows 上 PyInstaller 的控制台经常是 cp1252，中文不能把打包打崩。"""
    text = f"[limitpattern] {message}\n"
    stream = sys.stdout
    try:
        stream.write(text)
        stream.flush()
    except UnicodeEncodeError:
        buffer = getattr(stream, "buffer", None)
        payload = text.encode(getattr(stream, "encoding", None) or "utf-8", errors="replace")
        if buffer is not None:
            buffer.write(payload)
            buffer.flush()
        else:
            stream.write(payload.decode("ascii", errors="replace"))


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def _wanted_module(name: str) -> bool:
    parts = set(name.split("."))
    if parts & {"tests", "testing", "test"}:
        return False
    return not name.endswith((".testclient", ".pytest_plugin", ".mypy"))


def _try_submodules(package: str) -> list[str]:
    if importlib.util.find_spec(package) is None:
        _log(f"未安装 {package}，跳过子模块收集")
        return []
    try:
        return [name for name in collect_submodules(package) if _wanted_module(name)]
    except Exception as exc:  # 可选依赖缺失时不要让 spec 直接退出
        _log(f"collect_submodules({package}) 跳过：{exc}")
        return []


def _try_package_files(package: str, datas: list, binaries: list) -> None:
    if importlib.util.find_spec(package) is None:
        _log(f"未安装 {package}，跳过数据文件")
        return
    try:
        datas.extend(collect_data_files(package))
    except Exception as exc:
        _log(f"collect_data_files({package}) 跳过：{exc}")
    try:
        binaries.extend(collect_dynamic_libs(package))
    except Exception as exc:
        _log(f"collect_dynamic_libs({package}) 跳过：{exc}")


def _backend_modules() -> list[str]:
    backend = ROOT / "backend"
    if not backend.is_dir():
        _log("backend/ 不存在，打包 stub")
        return []
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    modules = _try_submodules("backend")
    if modules:
        _log(f"收集后端模块 {len(modules)} 个")
        return [name for name in modules if ".tests" not in name.split(".") and not name.startswith("backend.tests")]
    names: list[str] = []
    for path in backend.rglob("*.py"):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        relative = path.relative_to(ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(parts))
    _log(f"按目录枚举后端模块 {len(names)} 个")
    return names


def icon_path() -> str | None:
    icon = SPEC_DIR / "assets" / "icon.ico"
    return str(icon) if icon.is_file() else None


# 这些名字是字符串导入或可选依赖，模块图经常扫不到。没装就不要写进 hiddenimports。
OPTIONAL_HIDDEN = (
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.sql.default_comparator",
    "greenlet",
    "apscheduler.schedulers.background",
    "apscheduler.triggers.cron",
    "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
    "pydantic_settings",
    "tzdata",
    "tzlocal",
    "numpy",
    "httpx",
    "httpcore",
)

# 启动器用 importlib 按字符串加载后端，PyInstaller 扫不到。这些必须写死。
REQUIRED_BACKEND = (
    "backend",
    "backend.app",
    "backend.app.main",
    "backend.app.providers",
    "backend.app.providers.eastmoney",
    "backend.app.providers.tencent",
    "backend.app.providers.sina",
    "backend.app.providers.router",
    "backend.app.providers.markets",
    "backend.app.tasks",
    "backend.app.tasks.scheduler",
    "backend.app.services.sync_service",
    "backend.app.services.summary_service",
    "backend.app.services.similarity_service",
    "backend.app.services.export_service",
    "backend.app.services.history_backfill",
)


def _spec_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def _optional_hiddenimports() -> list[str]:
    return [name for name in OPTIONAL_HIDDEN if _spec_exists(name)]


def with_cluster() -> bool:
    return os.environ.get("LP_WITH_CLUSTER", "") == "1"


def analysis_kwargs() -> dict:
    _force_utf8_stdio()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    datas: list[tuple[str, str]] = []
    binaries: list[tuple[str, str]] = []
    hiddenimports = list(UVICORN_HIDDEN)

    for package in ("uvicorn", "fastapi", "starlette", "pydantic", "anyio", "opentelemetry", "websockets", "numpy", "httpx"):
        hiddenimports.extend(_try_submodules(package))

    stub_static = SPEC_DIR / "stub_static"
    datas.append((str(stub_static), "desktop/stub_static"))

    frontend = ROOT / "frontend" / "dist"
    if (frontend / "index.html").is_file():
        datas.append((str(frontend), "frontend/dist"))
        _log(f"打包前端 {frontend}")
    else:
        _log("frontend/dist 不存在，窗口使用 stub 页面")

    hiddenimports.extend(_backend_modules())
    if (ROOT / "backend").is_dir():
        hiddenimports.extend(REQUIRED_BACKEND)
        _log("已写入后端关键 hidden imports")
    hiddenimports.extend(_optional_hiddenimports())
    if with_cluster():
        # 聚类版：打进 scikit-learn。PyInstaller 的 sklearn/scipy hook 负责数据文件和二进制。
        hiddenimports.extend(
            [
                "sklearn",
                "sklearn.cluster",
                "sklearn.decomposition",
                "sklearn.metrics",
                "sklearn.utils._typedefs",
                "sklearn.utils._heap",
                "sklearn.utils._sorting",
                "sklearn.utils._vector_sentinel",
                "sklearn.neighbors._partition_nodes",
            ]
        )
        _log("聚类版：打包 scikit-learn")
    backend = ROOT / "backend"
    if backend.is_dir() and importlib.util.find_spec("backend") is not None:
        _try_package_files("backend", datas, binaries)
    for package in ("sqlalchemy", "apscheduler", "tzdata", "numpy", "httpx"):
        if importlib.util.find_spec(package) is not None:
            hiddenimports.extend(_try_submodules(package))
            _try_package_files(package, datas, binaries)

    hiddenimports.extend(
        [
            "desktop",
            "desktop.stub_app",
            "desktop.launcher",
            "stub_app",
        ]
    )

    if sys.platform == "win32":
        hiddenimports.extend(WINDOWS_GUI_HIDDEN)
        # webview / cffi / pystray / PIL 自带 PyInstaller hook。
        # 这里只补 pythonnet 和 clr_loader：它们的 DLL 不会被导入图发现。
        # 不要 collect_submodules("webview")，否则会在打包机上执行每个 GUI 后端。
        for package in ("pythonnet", "clr_loader"):
            _try_package_files(package, datas, binaries)
            hiddenimports.extend(_try_submodules(package))
    else:
        _log("非 Windows 构建，不收集 WebView2 / pythonnet")

    deduped_hidden = list(dict.fromkeys(hiddenimports))
    deduped_datas = list(dict.fromkeys(datas))
    deduped_binaries = list(dict.fromkeys(binaries))
    return {
        "scripts": [str(SPEC_DIR / "launcher.py")],
        "pathex": [str(ROOT), str(SPEC_DIR)],
        "binaries": deduped_binaries,
        "datas": deduped_datas,
        "hiddenimports": deduped_hidden,
        "hookspath": [],
        "hooksconfig": {},
        "runtime_hooks": [str(SPEC_DIR / "pyi_rth_pythonnet.py")],
        "excludes": [
            "tkinter",
            "PyQt5",
            "PyQt6",
            "PySide2",
            "PySide6",
            "gi",
            "pytest",
            "IPython",
            "matplotlib",
            "webview.platforms.android",
            "webview.platforms.cocoa",
            "webview.platforms.gtk",
            "webview.platforms.qt",
            "webview.platforms.cef",
            "fastapi.testclient",
            "pydantic.mypy",
            "pydantic.v1.mypy",
            "anyio.pytest_plugin",
        ]
        + ([] if with_cluster() else ["sklearn", "scipy"]),
        "noarchive": False,
        "optimize": 0,
    }
