"""三日涨停形态统计桌面启动器。

在子线程里启动 uvicorn，端口被占用就往后换，轮询 ``/api/health`` 成功后用
pywebview（WebView2）打开窗口。关闭窗口缩到托盘，托盘提供「显示 / 退出」。
窗口的 ``js_api.save_file(filename, text)`` 供页面「导出 TXT」：弹出系统另存为对话框，
按 UTF-8 BOM 写入，并打开 ``ALLOW_DOWNLOADS`` 作为浏览器下载的备用。
同一 Windows 会话只允许一个实例。日志写到 ``LP_DATA_DIR/logs/launcher.log``。

应用选择：

- 环境变量 ``LP_USE_STUB=1`` 时强制使用 stub。
- 否则优先 ``backend.app.main:app``；导入失败时退回 ``desktop.stub_app``。
- 静态页优先已有的 ``LP_STATIC_DIR``，其次 ``frontend/dist``，最后才是 stub 页面。
  选定的目录会写回 ``LP_STATIC_DIR``，供后端挂载。

数据目录：``LP_DATA_DIR``，缺省时 Windows 为 ``%LOCALAPPDATA%\\LimitPattern``，
其他系统为 ``~/.limit_pattern``。服务只绑定 ``127.0.0.1``。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import traceback
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_TITLE = "三日涨停形态统计"


def window_title() -> str:
    try:
        from backend.app import __version__
    except Exception:
        return APP_TITLE
    return f"{APP_TITLE} {__version__}"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOGGER_NAME = "limitpattern.launcher"
_LOG_HANDLER_MARK = "_limitpattern_file"
WEBVIEW2_MINIMUM = "86.0.622.0"
WEBVIEW2_GUIDS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # Runtime
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # Beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # Dev
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # Canary
)
WEBVIEW2_MISSING_MESSAGE = (
    "未检测到可用的 WebView2 内嵌窗口环境（需要安装 Microsoft Edge WebView2 Runtime，"
    "以及 .NET Framework 4.6.2 或更高版本）。\n\n"
    "将使用系统默认浏览器打开本程序。安装 WebView2 后重新启动，即可使用内嵌窗口。"
)

logger = logging.getLogger(LOGGER_NAME)


class AppState:
    def __init__(self) -> None:
        self.url = ""
        self.quitting = False
        self.browser_mode = False
        self.window = None
        self.icon = None
        self.server: ServerHandle | None = None
        self.control: ControlServer | None = None
        self.quit_event = threading.Event()
        self.show_requested = threading.Event()
        self.notifier: Notifier | None = None
        self.watcher: MonitorWatcher | None = None


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(sys.executable).resolve().parent


def ensure_import_path() -> None:
    """源码运行时把仓库根目录放进 sys.path，冻结后由 PyInstaller 提供导入。"""
    if is_frozen():
        return
    root = str(project_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def resolve_data_dir() -> Path:
    env = os.environ.get("LP_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local) / "LimitPattern"
        return Path.home() / "AppData" / "Local" / "LimitPattern"
    return Path.home() / ".limit_pattern"


def default_stub_static() -> Path:
    if is_frozen():
        for relative in (Path("desktop") / "stub_static", Path("stub_static")):
            candidate = bundle_root() / relative
            if candidate.is_dir():
                return candidate
    return Path(__file__).resolve().parent / "stub_static"


def resolve_static_dir() -> tuple[Path, str]:
    """返回 ``(目录, 来源)``。来源是 ``env`` / ``frontend`` / ``stub``。"""
    env = os.environ.get("LP_STATIC_DIR", "").strip()
    if env:
        path = Path(env).expanduser()
        if path.is_dir():
            return path, "env"
        logger.warning("LP_STATIC_DIR 不存在：%s", path)
    frontend = (bundle_root() if is_frozen() else project_root()) / "frontend" / "dist"
    if (frontend / "index.html").is_file():
        return frontend, "frontend"
    return default_stub_static(), "stub"


def stub_forced() -> bool:
    return os.environ.get("LP_USE_STUB", "").strip().lower() in {"1", "true", "yes", "on"}


def _load_stub_factory():
    ensure_import_path()
    try:
        from desktop.stub_app import create_app
    except ImportError:
        from stub_app import create_app
    return create_app


def _import_backend_app():
    import importlib

    ensure_import_path()
    try:
        module = importlib.import_module("backend.app.main")
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == "backend" or missing.startswith("backend."):
            logger.info("未找到后端模块 %s，使用 stub", missing or "backend")
            return None
        logger.exception("后端依赖缺失（%s），使用 stub", missing)
        return None
    except Exception:
        logger.exception("导入 backend.app.main 失败，使用 stub")
        return None
    factory = getattr(module, "create_app", None)
    app = factory() if callable(factory) else getattr(module, "app", None)
    if app is None:
        logger.error("backend.app.main 没有可用的应用，使用 stub")
        return None
    return app


def load_app(static_dir: Path) -> tuple[object, str]:
    if not stub_forced():
        backend_app = _import_backend_app()
        if backend_app is not None:
            logger.info("应用来源=backend")
            return backend_app, "backend"
    app = _load_stub_factory()(static_dir)
    logger.info("应用来源=stub")
    return app, "stub"


def setup_logging(data_dir: Path) -> Path:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "launcher.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        if getattr(handler, _LOG_HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    setattr(handler, _LOG_HANDLER_MARK, True)
    root.addHandler(handler)
    logger.info("日志文件 %s", log_path)
    return log_path


def _bypass_local_proxy() -> None:
    existing = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
    parts = [item.strip() for item in existing.split(",") if item.strip()]
    for host in ("127.0.0.1", "localhost"):
        if host not in parts:
            parts.append(host)
    joined = ",".join(parts)
    os.environ["NO_PROXY"] = joined
    os.environ["no_proxy"] = joined


def bind_server_socket(host: str, preferred: int, attempts: int = 50) -> tuple[socket.socket, int]:
    if preferred < 1 or preferred > 65535:
        preferred = DEFAULT_PORT
    last_error: Exception | None = None
    for offset in range(attempts):
        port = preferred + offset
        if port > 65535:
            break
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(128)
            return sock, port
        except OSError as exc:
            last_error = exc
            sock.close()
    raise RuntimeError(f"从 {host}:{preferred} 起 {attempts} 个端口都不可用") from last_error


class ServerHandle:
    def __init__(self, app: object, host: str, port: int, sock: socket.socket) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.sock = sock
        self.server = None
        self.thread: threading.Thread | None = None
        self.error = ""
        self._stopped = False

    def start(self) -> None:
        import uvicorn

        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.INFO)
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_config=None,
            access_log=False,
            log_level="info",
            lifespan="on",
            use_colors=False,
            workers=1,
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, name="limitpattern-uvicorn", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            assert self.server is not None
            self.server.run(sockets=[self.sock])
        except BaseException:
            self.error = traceback.format_exc()
            logger.error("uvicorn 异常退出\n%s", self.error)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        server = self.server
        thread = self.thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        if thread is not None and thread.is_alive() and server is not None:
            server.force_exit = True
            thread.join(timeout=3)
        try:
            self.sock.close()
        except OSError:
            pass


def _open_local(url: str, timeout: float) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            return int(response.status), text
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body


def check_homepage(host: str, port: int, static_kind: str) -> None:
    """确认首页能打开。前端包还要能把脚本拉下来。"""
    page_url = f"http://{host}:{port}/"
    status, html = _open_local(page_url, 5.0)
    if status != 200:
        raise RuntimeError(f"首页 HTTP {status}")
    if "<html" not in html.lower():
        raise RuntimeError("首页不是 HTML")
    if static_kind == "frontend" and 'id="app"' not in html:
        raise RuntimeError("首页不是前端页面")
    for match in re.finditer(r"""<script[^>]+src=["']([^"']+)["']""", html, flags=re.IGNORECASE):
        src = match.group(1)
        if not src.startswith("/") or src.startswith("//"):
            continue
        asset_status, asset_body = _open_local(f"http://{host}:{port}{src}", 5.0)
        if asset_status != 200 or not asset_body.strip():
            raise RuntimeError(f"首页脚本没有加载到：{src}")
        break
    logger.info("首页检查通过 %s", page_url)


def wait_until_healthy(host: str, port: int, thread: threading.Thread | None, timeout: float = 30.0) -> dict:
    url = f"http://{host}:{port}/api/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    deadline = _monotonic() + timeout
    last_error = "尚未连接"
    while _monotonic() < deadline:
        if thread is not None and not thread.is_alive():
            raise RuntimeError(f"服务线程已退出，{url} 没有就绪")
        try:
            with opener.open(request, timeout=1.5) as response:
                raw = response.read().decode("utf-8")
                if response.status != 200:
                    last_error = f"HTTP {response.status}"
                else:
                    payload = json.loads(raw)
                    if payload.get("status") == "ok":
                        return payload
                    last_error = raw
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)
        _sleep(0.2)
    raise TimeoutError(f"等待 {url} 超时：{last_error}")


def _monotonic() -> float:
    import time

    return time.monotonic()


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


class SingleInstance:
    """Windows 用会话级互斥量，所有平台再用数据目录里的文件锁。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._file = None
        self._mutex = None
        self._kernel32 = None

    def try_acquire(self) -> bool:
        if sys.platform == "win32" and not self._acquire_mutex():
            return False
        if not self._acquire_file():
            self.release()
            return False
        return True

    def _acquire_mutex(self) -> bool:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, "Local\\LimitPattern.SingleInstance")
        if not handle:
            raise OSError("CreateMutexW 失败")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        self._mutex = handle
        self._kernel32 = kernel32
        return True

    def _acquire_file(self) -> bool:
        path = self.data_dir / "instance.lock"
        handle = open(path, "a+b")
        self._file = handle
        try:
            if handle.tell() == 0 and handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is not None:
            try:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass
        if self._mutex is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(self._mutex)
            self._mutex = None


class ControlServer:
    """第二个实例连上这个端口并发送 SHOW，用来唤起已有窗口。"""

    def __init__(self, data_dir: Path, on_show) -> None:
        self.data_dir = data_dir
        self.on_show = on_show
        self.quit_event = threading.Event()
        self.port = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self, http_port: int, url: str) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((DEFAULT_HOST, 0))
        sock.listen(5)
        sock.settimeout(0.5)
        self._sock = sock
        self.port = sock.getsockname()[1]
        _write_json(
            self.data_dir / "instance.json",
            {"pid": os.getpid(), "port": http_port, "control_port": self.port, "url": url},
        )
        self._thread = threading.Thread(target=self._loop, name="limitpattern-control", daemon=True)
        self._thread.start()
        return self.port

    def _loop(self) -> None:
        assert self._sock is not None
        while not self.quit_event.is_set():
            try:
                connection, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                try:
                    data = connection.recv(64)
                except OSError:
                    continue
                if data.startswith(b"SHOW"):
                    try:
                        self.on_show()
                    except Exception:
                        logger.exception("处理显示请求失败")

    def stop(self) -> None:
        self.quit_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _remove_instance_file(data_dir: Path) -> None:
    path = data_dir / "instance.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if int(info.get("pid", -1)) != os.getpid():
        return
    path.unlink(missing_ok=True)


def signal_existing_instance(data_dir: Path) -> bool:
    path = data_dir / "instance.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        port = int(info["control_port"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.info("已有实例在运行，但没有可通知的控制端口")
        return False
    try:
        with socket.create_connection((DEFAULT_HOST, port), timeout=1.0) as connection:
            connection.sendall(b"SHOW\n")
    except OSError:
        logger.info("已有实例在运行，通知它显示窗口失败")
        return False
    logger.info("已通知正在运行的实例显示窗口")
    return True


def version_at_least(version: str, minimum: str = WEBVIEW2_MINIMUM) -> bool:
    def parse(text: str) -> tuple[int, ...] | None:
        stripped = text.strip()
        if not stripped or stripped in {"0", "0.0.0.0"}:
            return None
        parts: list[int] = []
        for item in stripped.split("."):
            digits = "".join(ch for ch in item if ch.isdigit())
            if not digits:
                return None
            parts.append(int(digits))
        return tuple(parts) if parts else None

    current = parse(version)
    target = parse(minimum)
    if current is None or target is None:
        return False
    width = max(len(current), len(target))
    current = current + (0,) * (width - len(current))
    target = target + (0,) * (width - len(target))
    return current >= target


def webview2_installed() -> bool:
    if sys.platform != "win32":
        return False
    import platform
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
        ) as key:
            release, _kind = winreg.QueryValueEx(key, "Release")
        if int(release) < 394802:  # .NET Framework 4.6.2
            return False
    except OSError:
        return False

    machine = platform.machine().lower()
    roots = [(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients")]
    if machine in {"x86", "i386"}:
        roots.append((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"))
    else:
        roots.append((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"))
        roots.append((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"))
    for hive, root in roots:
        for guid in WEBVIEW2_GUIDS:
            try:
                with winreg.OpenKey(hive, root + "\\" + guid) as key:
                    revision, _kind = winreg.QueryValueEx(key, "pv")
            except OSError:
                continue
            if version_at_least(str(revision)):
                return True
    return False


def explain_webview_unavailable(force_browser: bool) -> str | None:
    """返回 None 表示使用内嵌窗口；空字符串表示改用浏览器但不弹窗。"""
    if force_browser:
        return ""
    if sys.platform == "win32" and not webview2_installed():
        return WEBVIEW2_MISSING_MESSAGE
    return None


def window_close_action(quitting: bool, tray_ready: bool) -> str:
    if quitting or not tray_ready:
        return "destroy"
    return "hide"


def notify(message: str, title: str = APP_TITLE) -> None:
    logger.info("提示：%s", message.replace("\n", " "))
    if sys.platform != "win32":
        print(message, file=sys.stderr)
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.MessageBoxW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, message, title, 0x40 | 0x00010000)
    except Exception:
        logger.exception("弹出提示框失败")
        print(message, file=sys.stderr)


def open_browser(url: str) -> None:
    logger.info("使用默认浏览器打开 %s", url)
    webbrowser.open(url)


def make_tray_image():
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 61, 61), radius=12, fill=(11, 15, 20, 255))
    draw.rectangle((31, 10, 33, 54), fill=(226, 61, 61, 255))
    draw.rectangle((26, 20, 38, 46), fill=(226, 61, 61, 255))
    return image


def show_ui(state: AppState) -> None:
    if state.browser_mode:
        if state.url:
            open_browser(state.url)
        return
    window = state.window
    if window is None:
        state.show_requested.set()
        return
    try:
        window.show()
    except Exception:
        logger.exception("显示窗口失败")
        if state.url:
            open_browser(state.url)


def stop_tray(state: AppState) -> None:
    icon = state.icon
    state.icon = None
    if icon is None:
        return
    try:
        icon.stop()
    except Exception:
        logger.exception("停止托盘失败")


def request_quit(state: AppState) -> None:
    if state.quitting:
        state.quit_event.set()
        return
    state.quitting = True
    logger.info("退出程序")
    if state.server is not None:
        state.server.stop()
    window = state.window
    if window is not None:
        try:
            window.destroy()
        except Exception:
            logger.exception("销毁窗口失败")
    state.quit_event.set()
    stop_tray(state)


def start_tray(state: AppState) -> bool:
    try:
        import pystray

        image = make_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("显示", lambda _icon, _item: show_ui(state), default=True),
            pystray.MenuItem("退出", lambda _icon, _item: request_quit(state)),
        )
        icon = pystray.Icon("LimitPattern", image, APP_TITLE, menu)
        state.icon = icon
        threading.Thread(target=icon.run, name="limitpattern-tray", daemon=True).start()
        logger.info("托盘图标已创建")
        return True
    except Exception:
        logger.exception("托盘图标创建失败")
        state.icon = None
        return False


def sanitize_export_filename(filename: str) -> str:
    """只保留文件名，避免页面传入的路径写到别的目录。"""
    raw = str(filename or "").replace("\\", "/").split("/")[-1].strip().replace("\x00", "")
    cleaned = "".join("_" if char in '<>:"|?*' else char for char in raw).strip(" .")
    if cleaned in {"", ".", ".."}:
        cleaned = "export.txt"
    if Path(cleaned).suffix == "":
        cleaned += ".txt"
    return cleaned


def default_save_directory(home: Path | None = None) -> Path:
    """优先桌面，其次下载目录，都没有就用用户主目录。"""
    root = Path(home) if home is not None else Path.home()
    for name in ("Desktop", "桌面"):
        candidate = root / name
        if candidate.is_dir():
            return candidate
    for name in ("Downloads", "下载"):
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return root


def save_text_as(filename: str, text: str, ask_path) -> str | None:
    """弹出另存为并写入 UTF-8 BOM。取消时返回 None，成功返回保存路径。"""
    name = sanitize_export_filename(filename)
    directory = default_save_directory()
    chosen = ask_path(directory, name)
    if not chosen:
        logger.info("导出已取消")
        return None
    path = Path(str(chosen))
    if path.suffix == "":
        path = path.with_suffix(".txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("" if text is None else str(text), encoding="utf-8-sig", newline="\n")
    logger.info("已导出 %s", path)
    return str(path)


_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$title = [Environment]::GetEnvironmentVariable('LP_TOAST_TITLE')
$body = [Environment]::GetEnvironmentVariable('LP_TOAST_BODY')
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual><audio src="ms-winsoundevent:Notification.Default"/></toast>')
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($title)) | Out-Null
$nodes.Item(1).AppendChild($xml.CreateTextNode($body)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def windows_toast(title: str, message: str) -> bool:
    """用系统自带 PowerShell 弹 Windows 通知。失败返回 False，由托盘气泡兜底。"""
    if sys.platform != "win32":
        return False
    import subprocess

    env = dict(os.environ, LP_TOAST_TITLE=str(title)[:120], LP_TOAST_BODY=str(message)[:400])
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", _TOAST_SCRIPT],
            env=env,
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        logger.exception("Windows 通知没有弹出")
        return False
    if result.returncode != 0:
        logger.warning("Windows 通知失败：%s", result.stderr.decode("utf-8", "ignore")[:300])
        return False
    return True


def message_box(title: str, message: str) -> bool:
    """最后兜底：置顶的系统消息框，放在后台线程里弹，不阻塞调用方。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        def run() -> None:
            try:
                ctypes.windll.user32.MessageBeep(0x30)
                ctypes.windll.user32.MessageBoxW(None, str(message), str(title), 0x30 | 0x40000 | 0x1000)
            except Exception:
                logger.exception("消息框失败")

        threading.Thread(target=run, name="limitpattern-msgbox", daemon=True).start()
        return True
    except Exception:
        return False


class Notifier:
    """桌面提醒，按顺序兜底：Windows 通知 → 托盘气泡 → 置顶消息框 → 只记日志。同一个 key 只提醒一次。"""

    def __init__(self, state: "AppState | None" = None, toast=None, box=None) -> None:
        self.state = state
        self._toast = toast or windows_toast
        self._box = box or message_box
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def remember(self, key: str) -> None:
        with self._lock:
            self._seen.add(key)

    def notify(self, title: str, message: str, key: str | None = None) -> str:
        if key:
            with self._lock:
                if key in self._seen:
                    return "duplicate"
                self._seen.add(key)
        logger.info("盘中提醒：%s %s", title, message)
        if self._toast(title, message):
            return "toast"
        icon = getattr(self.state, "icon", None) if self.state is not None else None
        if icon is not None:
            try:
                icon.notify(str(message), str(title))
                return "balloon"
            except Exception:
                logger.exception("托盘气泡失败")
        if self._box(title, message):
            return "msgbox"
        return "log"


def format_event(event: dict) -> tuple[str, str]:
    level = event.get("level")
    pct = event.get("pct")
    pct_text = "" if pct is None else f"{float(pct):+.2f}%"
    head = "涨幅超 6% 报警" if level == "alert" else "涨幅超 3% 提醒"
    rank = event.get("rank")
    rank_text = f"（昨日 AI 榜第 {rank} 名）" if rank else ""
    return head, f"{event.get('code', '')} {event.get('name', '')} {pct_text}{rank_text}".strip()


def event_key(date_text: str, event: dict) -> str:
    return f"{date_text}:{event.get('code')}:{event.get('level')}"


class MonitorWatcher:
    """后台读 /api/monitor/events，页面没打开也会提醒。
    非报警时段后端不拉行情；这里也放慢到每分钟看一次。已提醒的游标和键按天存盘，重启不重复弹。"""

    IDLE_INTERVAL = 60.0

    def __init__(self, base_url: str, notifier: Notifier, interval: float = 5.0, fetch=None, state_path: Path | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.notifier = notifier
        self.interval = interval
        self.last_id = 0
        self.active = True
        self.day = ""
        self.state_path = state_path
        self._fetch = fetch or self._http_json
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.day = str(data.get("date") or "")
            self.last_id = int(data.get("last_id") or 0)
            for key in data.get("keys") or []:
                self.notifier.remember(str(key))
        except Exception as exc:
            logger.debug("监控游标读取失败：%s", exc)

    def _save_state(self, keys: list[str]) -> None:
        if not self.state_path:
            return
        try:
            old: list[str] = []
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if data.get("date") == self.day:
                    old = list(data.get("keys") or [])
            payload = {"date": self.day, "last_id": self.last_id, "keys": sorted(set(old + keys))}
            self.state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.debug("监控游标保存失败：%s", exc)

    def _http_json(self, url: str) -> dict:
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def poll_once(self) -> int:
        payload = self._fetch(f"{self.base_url}/api/monitor/events?since={self.last_id}")
        count = 0
        day = str(payload.get("date") or "")
        if day and day != self.day:
            self.day = day
        self.active = payload.get("alerts_active") is not False
        keys = []
        for event in payload.get("items") or []:
            title, message = format_event(event)
            key = event_key(day, event)
            keys.append(key)
            if self.notifier.notify(title, message, key) != "duplicate":
                count += 1
        before = self.last_id
        self.last_id = max(self.last_id, int(payload.get("last_id") or 0))
        if keys or self.last_id != before:
            self._save_state(keys)
        return count

    def _loop(self) -> None:
        wait = self.interval
        while not self._stop.wait(wait):
            try:
                self.poll_once()
            except Exception as exc:
                logger.debug("监控事件读取失败：%s", exc)
            wait = self.interval if self.active else max(self.interval, self.IDLE_INTERVAL)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="limitpattern-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()


class ExportApi:
    """pywebview js_api。页面调用 ``window.pywebview.api.save_file(filename, text)``，
    盘中提醒调用 ``window.pywebview.api.notify(title, message, key)``。"""

    # 注意：pywebview 会把 js_api 对象的公开属性递归暴露给页面，并在 GUI 线程上遍历。
    # 公开属性里放 Window / Notifier（它又连着 AppState、托盘、uvicorn）会让遍历走进巨大的对象图，
    # 窗口直接「未响应」。所以这里只留公开方法，状态全部用下划线私有属性。

    def __init__(self, notifier: Notifier | None = None) -> None:
        self._window = None
        self._notifier = notifier

    def _attach(self, window) -> None:
        self._window = window

    def notify_test(self, level: str = "warn") -> str:
        """「测试提醒」按钮：同步走一遍兜底链，返回实际用上的方式（toast/balloon/msgbox/log）。"""
        if self._notifier is None:
            self._notifier = Notifier()
        title = "涨幅超 6% 报警（测试）" if level == "alert" else "涨幅超 3% 提醒（测试）"
        return self._notifier.notify(title, "600000 示例股票 +6.20%（这是一条测试提醒）" if level == "alert" else "600000 示例股票 +3.10%（这是一条测试提醒）")

    def notify(self, title: str, message: str, key: str | None = None) -> str:
        """立刻返回，真正弹通知放到后台线程，绝不占用页面调用线程。"""
        if self._notifier is None:
            self._notifier = Notifier()
        notifier = self._notifier
        threading.Thread(
            target=notifier.notify, args=(title, message, key), name="limitpattern-notify", daemon=True
        ).start()
        return "queued"

    def save_file(self, filename: str, text: str) -> str | None:
        import webview

        window = self._window

        def ask_path(directory: Path, name: str) -> str | None:
            if window is None:
                raise RuntimeError("窗口还没准备好，不能导出")
            selected = window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=str(directory),
                save_filename=name,
                file_types=(("Markdown (*.md)", "所有文件 (*.*)") if name.lower().endswith(".md") else ("文本文件 (*.txt)", "所有文件 (*.*)")),
            )
            if not selected:
                return None
            return str(selected[0])

        return save_text_as(filename, text, ask_path)

    def open_folder(self, path: str) -> bool:
        """导出后「打开所在文件夹」。后台线程启动资源管理器，不占 GUI 线程。"""
        target = Path(path)
        folder = target.parent if target.suffix else target
        if not folder.exists():
            return False

        def run() -> None:
            try:
                if sys.platform == "win32":
                    import subprocess

                    if target.is_file():
                        subprocess.Popen(["explorer", "/select,", str(target)])
                    else:
                        os.startfile(str(folder))  # type: ignore[attr-defined]
                else:
                    import subprocess

                    subprocess.Popen(["xdg-open", str(folder)])
            except Exception:
                logger.exception("打开文件夹失败")

        threading.Thread(target=run, name="limitpattern-open-folder", daemon=True).start()
        return True


def enable_webview_downloads(settings: dict | None = None) -> dict:
    """允许页面触发的文件下载，作为 save_file 之外的备用。"""
    if settings is None:
        import webview

        settings = webview.settings
    settings["ALLOW_DOWNLOADS"] = True
    return settings


def _hide_window(state: AppState) -> None:
    window = state.window
    if window is None:
        return
    try:
        window.hide()
        logger.info("窗口已隐藏到托盘")
    except Exception:
        logger.exception("隐藏窗口失败")


def run_webview(state: AppState) -> None:
    import webview

    def on_closing() -> bool:
        action = window_close_action(state.quitting, state.icon is not None)
        if action == "destroy":
            return True
        # 在关闭回调里同步 hide() 会卡住 WinForms 消息循环，所以丢到别的线程。
        threading.Thread(target=_hide_window, args=(state,), name="limitpattern-hide", daemon=True).start()
        return False

    enable_webview_downloads()
    export_api = ExportApi(state.notifier)
    window = webview.create_window(
        window_title(),
        state.url,
        js_api=export_api,
        width=1280,
        height=800,
        min_size=(960, 640),
        background_color="#0b0f14",
        text_select=True,
        confirm_close=False,
    )
    if window is None:
        raise RuntimeError("pywebview 没有创建窗口")
    export_api._attach(window)
    state.window = window
    window.events.closing += on_closing
    logger.info("启动 pywebview，加载 %s", state.url)
    webview.start()


def verify_gui_imports() -> None:
    if is_frozen():
        root = bundle_root()
        for relative in (
            "pythonnet/runtime/Python.Runtime.dll",
            "clr_loader/ffi/dlls/amd64/ClrLoader.dll",
            "webview/lib/Microsoft.Web.WebView2.Core.dll",
            "webview/lib/Microsoft.Web.WebView2.WinForms.dll",
            "webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
        ):
            path = root / relative
            logger.info("资源%s %s", "存在" if path.is_file() else "缺失", path)
        logger.info("PYTHONNET_PYDLL=%s", os.environ.get("PYTHONNET_PYDLL", ""))
    logger.info("开始导入 WebView2 / pythonnet")
    import clr  # noqa: F401

    logger.info("已导入 clr")
    import webview

    logger.info("已导入 webview %s", getattr(webview, "__version__", ""))
    import webview.platforms.edgechromium  # noqa: F401

    logger.info("已导入 edgechromium")
    import webview.platforms.winforms  # noqa: F401

    logger.info("已导入 winforms")


def _wait_until_quit(state: AppState) -> None:
    try:
        while not state.quit_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        request_quit(state)


def launch_ui(state: AppState, force_browser: bool) -> None:
    reason = explain_webview_unavailable(force_browser)
    tray_ready = start_tray(state)
    if reason is not None:
        state.browser_mode = True
        if reason:
            notify(reason)
        open_browser(state.url)
        if tray_ready:
            _wait_until_quit(state)
        else:
            logger.warning("没有托盘图标。结束本进程即可退出，日志里有数据目录。")
            _wait_until_quit(state)
        return
    try:
        run_webview(state)
    except Exception as exc:
        logger.exception("内嵌窗口启动失败")
        if state.quitting:
            return
        state.browser_mode = True
        notify("内嵌窗口启动失败，将改用默认浏览器打开。\n" + str(exc))
        open_browser(state.url)
        _wait_until_quit(state)


def _install_excepthook(smoke: bool, data_dir: Path) -> None:
    def hook(exc_type, exc, tb) -> None:
        if exc_type is KeyboardInterrupt:
            return
        logger.critical("未捕获异常", exc_info=(exc_type, exc, tb))
        if not smoke:
            notify(f"程序出现未捕获错误。请查看日志：{data_dir / 'logs' / 'launcher.log'}")

    sys.excepthook = hook


def _serve_and_open(args: argparse.Namespace, data_dir: Path, state: AppState) -> int:
    static_dir, static_kind = resolve_static_dir()
    os.environ["LP_STATIC_DIR"] = str(static_dir)
    logger.info("静态目录（%s）%s", static_kind, static_dir)
    app, _source = load_app(static_dir)
    preferred = args.port if args.port and args.port > 0 else DEFAULT_PORT
    sock, port = bind_server_socket(DEFAULT_HOST, preferred)
    if port != preferred:
        logger.info("端口 %s 已被占用，改用 %s", preferred, port)
    os.environ["LP_PORT"] = str(port)
    state.url = f"http://{DEFAULT_HOST}:{port}/"
    server = ServerHandle(app, DEFAULT_HOST, port, sock)
    state.server = server
    server.start()
    timeout = 120.0 if is_frozen() else 20.0
    payload = wait_until_healthy(DEFAULT_HOST, port, server.thread, timeout=timeout)
    logger.info("健康检查通过 %s", json.dumps(payload, ensure_ascii=False))
    try:
        from backend.app.algorithms.clustering import sklearn_available

        logger.info("聚类依赖%s", "可用" if sklearn_available() else "未安装")
    except Exception:
        logger.info("聚类依赖未安装")
    check_homepage(DEFAULT_HOST, port, static_kind)
    logger.info("服务已就绪 %s", state.url)
    control = ControlServer(data_dir, lambda: show_ui(state))
    state.control = control
    control.start(port, state.url)
    if args.check_gui or (args.smoke and is_frozen() and sys.platform == "win32"):
        verify_gui_imports()
    if args.smoke:
        return 0
    state.notifier = Notifier(state)
    state.watcher = MonitorWatcher(state.url, state.notifier, state_path=data_dir / "monitor_notified.json")
    state.watcher.start()
    launch_ui(state, args.open_browser)
    return 0


def _run(args: argparse.Namespace, data_dir: Path) -> int:
    logger.info("启动 pid=%s frozen=%s argv=%s", os.getpid(), is_frozen(), sys.argv)
    _install_excepthook(args.smoke, data_dir)
    instance = SingleInstance(data_dir)
    if not instance.try_acquire():
        logger.info("已有实例在运行")
        signal_existing_instance(data_dir)
        if args.smoke:
            logger.error("smoke 检测到已有实例，无法自检")
            return 2
        notify("三日涨停形态统计已在运行。")
        return 0
    state = AppState()
    try:
        return _serve_and_open(args, data_dir, state)
    except KeyboardInterrupt:
        logger.info("收到中断，准备退出")
        state.quitting = True
        return 0
    finally:
        if state.control is not None:
            state.control.stop()
        if state.server is not None:
            state.server.stop()
        stop_tray(state)
        _remove_instance_file(data_dir)
        instance.release()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="LimitPattern", description="三日涨停形态统计桌面启动器")
    parser.add_argument("--smoke", action="store_true", help="启动服务，检查 /api/health 和首页后退出")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="首选端口，被占用时自动 +1")
    parser.add_argument("--open-browser", action="store_true", help="不使用内嵌窗口，直接打开系统浏览器")
    parser.add_argument("--check-gui", action="store_true", help="额外导入 WebView2 / pythonnet（Windows 打包自检）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if sys.version_info < (3, 11):
        print("需要 Python 3.11 或更高版本", file=sys.stderr)
        return 1
    data_dir = resolve_data_dir()
    os.environ["LP_DATA_DIR"] = str(data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"无法创建数据目录 {data_dir}：{exc}", file=sys.stderr)
        return 1
    _bypass_local_proxy()
    try:
        setup_logging(data_dir)
    except Exception as exc:
        print(f"无法创建日志：{exc}", file=sys.stderr)
        return 1
    try:
        return _run(args, data_dir)
    except Exception:
        logger.exception("启动失败")
        if not args.smoke:
            notify(f"程序启动失败。请查看日志：{data_dir / 'logs' / 'launcher.log'}")
        return 1


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
