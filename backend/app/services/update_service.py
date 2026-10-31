"""在线更新。

- 检查：GitHub Releases（公开仓库，无需登录）`/repos/peekaboo789/limit-pattern/releases/latest`；
  拿不到时退回读分支上的 backend/app/__init__.py 版本号。
- 桌面版（Windows 绿色版）：下载 Release 里的 zip（可设镜像前缀，GitHub 慢时用）→ 校验 sha256 → 解压到数据目录 updates/ →
  先备份数据 → 用新版程序 `--apply-update` 等旧进程退出后替换程序目录（旧目录改名 .old 留作回滚，失败自动还原）→ 启动新版。
  数据目录（%LOCALAPPDATA%\\LimitPattern）不在程序目录里，替换程序不碰数据。
- Docker：容器不能安全地替换自己的镜像，只显示新版本和 `docker compose pull && docker compose up -d`（或用 Watchtower）。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time as _time
import zipfile
from pathlib import Path

import httpx

from backend.app import __version__
from backend.app.core.config import data_dir, read_settings_file
from backend.app.core.logging import get_logger

logger = get_logger("limit_pattern.update")
REPO = "peekaboo789/limit-pattern"
BRANCH = "cursor/limit-pattern-system-8abb"
ASSET = "LimitPattern-cluster-win64.zip"
DOCKER_CMD = "docker compose pull && docker compose up -d"
CACHE_TTL = 600

_HTTP: dict = {"client": None}
_CACHE: dict = {"at": 0.0, "info": None}
_LOCK = threading.Lock()
STATE: dict = {"phase": "idle", "message": "", "downloaded": 0, "total": 0, "version": None, "error": ""}


def set_http_client(client: httpx.Client | None) -> None:
    _HTTP["client"] = client
    _CACHE.update(at=0.0, info=None)


def _http() -> httpx.Client:
    if _HTTP["client"] is None:
        _HTTP["client"] = httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": f"LimitPattern/{__version__}"})
    return _HTTP["client"]


def vtuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", str(v or "").lstrip("vV"))
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(latest: str, current: str = __version__) -> bool:
    return vtuple(latest) > vtuple(current)


def mode() -> str:
    if os.environ.get("LP_HEADLESS", "0") == "1":
        return "docker"
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        return "desktop"
    return "source"


def parse_release(payload: dict) -> dict:
    tag = str(payload.get("tag_name") or "")
    assets = {a.get("name"): a.get("browser_download_url") for a in payload.get("assets") or []}
    return {
        "latest": tag.lstrip("vV"),
        "tag": tag,
        "notes": str(payload.get("body") or "").strip()[:6000],
        "published_at": payload.get("published_at"),
        "url": payload.get("html_url") or f"https://github.com/{REPO}/releases",
        "zip_url": assets.get(ASSET),
        "sha256_url": assets.get(ASSET + ".sha256"),
    }


def check(force: bool = False) -> dict:
    with _LOCK:
        if not force and _CACHE["info"] and _time.monotonic() - _CACHE["at"] < CACHE_TTL:
            return dict(_CACHE["info"])
    info = {"current": __version__, "mode": mode(), "docker_command": DOCKER_CMD, "error": ""}
    try:
        r = _http().get(f"https://api.github.com/repos/{REPO}/releases/latest", headers={"Accept": "application/vnd.github+json"})
        if r.status_code == 200:
            info.update(parse_release(r.json()))
        else:
            raise RuntimeError(f"HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        try:
            r = _http().get(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/backend/app/__init__.py")
            m = re.search(r'__version__ = "(.+?)"', r.text)
            if not m:
                raise RuntimeError("没读到版本号")
            info.update(latest=m.group(1), tag="v" + m.group(1), notes="", url=f"https://github.com/{REPO}/releases", zip_url=None, sha256_url=None)
        except Exception as exc2:  # noqa: BLE001
            info.update(latest=None, error=f"连不上 GitHub（{type(exc).__name__} / {type(exc2).__name__}），稍后再试或设置镜像")
    info["newer"] = bool(info.get("latest")) and is_newer(info["latest"])
    info["can_auto"] = info["mode"] == "desktop" and bool(info.get("zip_url")) and info["newer"]
    with _LOCK:
        _CACHE.update(at=_time.monotonic(), info=dict(info))
    return info


def wecom_text(info: dict) -> tuple[str, str]:
    if info.get("error") and not info.get("latest"):
        return "🔄 检查更新", f"当前 v{info['current']}\n{info['error']}"
    if not info.get("newer"):
        return "🔄 检查更新", f"已是最新版 v{info['current']}"
    lines = [f"当前 v{info['current']} → 最新 v{info['latest']}"]
    if info["mode"] == "docker":
        lines.append(f"在服务器 compose 目录执行：\n{DOCKER_CMD}\n数据在 /data 卷里，升级会保留。")
    else:
        lines.append("在电脑上打开 设置 → 在线更新 → 下载并更新（数据会先备份并保留）。")
    if info.get("notes"):
        lines.append("更新内容：\n" + info["notes"][:600])
    return "🆕 有新版本", "\n".join(lines)


# ---------- 桌面版下载 / 校验 / 解压 ----------
def updates_dir() -> Path:
    path = data_dir() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _set(**kw) -> None:
    STATE.update(kw)


def status() -> dict:
    return dict(STATE)


def _candidates(url: str) -> list[str]:
    mirror = str(read_settings_file().get("update_mirror") or "").strip()
    urls = [url]
    if mirror:
        urls.append(mirror.rstrip("/") + "/" + url)  # 常见 GitHub 加速前缀写法：https://镜像/https://github.com/...
    return urls


def _download(url: str, dest: Path) -> None:
    last: Exception | None = None
    for candidate in _candidates(url):
        try:
            with _http().stream("GET", candidate, timeout=httpx.Timeout(30.0, read=120.0)) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                _set(total=total, downloaded=0)
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 16):
                        fh.write(chunk)
                        STATE["downloaded"] += len(chunk)
                tmp.replace(dest)
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("下载失败（%s），换下一个地址", type(exc).__name__)
    raise RuntimeError(f"下载失败：{last}")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expected_sha(text: str) -> str:
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text or "")
    if not m:
        raise RuntimeError("sha256 文件格式不对")
    return m.group(1).lower()


def find_program(root: Path) -> Path | None:
    for exe in root.rglob("LimitPattern.exe"):
        if (exe.parent / "_internal").is_dir():
            return exe.parent
    return None


def prepare(info: dict) -> Path:
    """下载 + 校验 + 解压，返回新版程序目录。"""
    try:
        return _prepare(info)
    except Exception as exc:
        _set(phase="error", message=str(exc), error=str(exc))
        raise


def _prepare(info: dict) -> Path:
    version = info["latest"]
    work = updates_dir() / f"v{version}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    zpath = work / ASSET
    _set(phase="download", message=f"正在下载 v{version}", version=version, error="")
    _download(info["zip_url"], zpath)
    _set(phase="verify", message="校验 sha256")
    if not info.get("sha256_url"):
        raise RuntimeError("Release 里没有 sha256 校验文件，为安全起见不自动更新")
    sha_file = work / "sha256.txt"
    _download(info["sha256_url"], sha_file)
    want, got = expected_sha(sha_file.read_text(encoding="utf-8", errors="replace")), sha256_of(zpath)
    if want != got:
        raise RuntimeError("sha256 不一致，下载的文件可能损坏或被篡改，已放弃")
    _set(phase="extract", message="解压")
    out = work / "extract"
    with zipfile.ZipFile(zpath) as zf:
        for member in zf.namelist():
            target = (out / member).resolve()
            if out.resolve() not in target.parents and target != out.resolve():
                raise RuntimeError("压缩包里有不安全的路径")
        zf.extractall(out)
    program = find_program(out)
    if program is None:
        raise RuntimeError("压缩包里没找到 LimitPattern.exe")
    _set(phase="ready", message=f"v{version} 已下载并校验，点「立即更新」重启替换")
    return program


def start_download() -> dict:
    if STATE["phase"] in ("download", "verify", "extract"):
        return status()
    info = check(force=True)
    if not info.get("newer"):
        raise ValueError("已是最新版")
    if not info.get("zip_url"):
        raise ValueError("最新 Release 里没有 Windows 程序包")

    def run():
        try:
            STATE["program"] = str(prepare(info))
        except Exception as exc:  # noqa: BLE001
            _set(phase="error", message=str(exc), error=str(exc))

    threading.Thread(target=run, name="update-download", daemon=True).start()
    return status()


def install_dir() -> Path:
    return Path(sys.executable).resolve().parent


def apply() -> dict:
    """备份数据，启动新版的 --apply-update，然后退出当前进程。"""
    if mode() != "desktop":
        raise ValueError("只有 Windows 桌面版能一键替换程序")
    program = STATE.get("program")
    if STATE["phase"] != "ready" or not program:
        raise ValueError("还没有下载好新版本")
    target = install_dir()
    if target == data_dir() or target in data_dir().resolve().parents:
        raise ValueError("数据目录在程序目录里面，自动替换会有风险。请先把 LP_DATA_DIR 改到程序目录外")
    from backend.app.core.database import wal_checkpoint
    from backend.app.services import backups

    wal_checkpoint("update")
    backups.make_backup(f"pre-update-v{STATE.get('version')}")
    exe = Path(program) / "LimitPattern.exe"
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe), "--apply-update", "--target", str(target), "--pid", str(os.getpid())],
                     cwd=str(Path(program)), creationflags=flags, close_fds=True)
    _set(phase="applying", message="正在替换程序，马上自动重启")
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return status()


def swap_install(target: Path, source: Path) -> Path:
    """用 source 目录替换 target 目录。旧目录改名为 <target>.old（下次更新前一直留着，可手动回滚）。
    任何一步失败都把旧目录还原并抛出。返回 .old 路径。"""
    target, source = Path(target), Path(source)
    if not (source / "LimitPattern.exe").is_file() and not any(source.iterdir()):
        raise RuntimeError("新版目录是空的")
    old = target.with_name(target.name + ".old")
    if old.exists():
        shutil.rmtree(old)
    target.rename(old)
    try:
        shutil.copytree(source, target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        old.rename(target)
        raise
    return old
