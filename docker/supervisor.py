"""容器入口：小型监督进程（只用标准库，随镜像发布，不会被程序内更新替换）。

1. 有待安装的更新包（/data/updates/pending.json）→ 校验 sha256 → 解压到 /data/app/<ver> → 核对依赖哈希 → 导入自检 → 设为当前。
2. 选择运行哪份代码：程序内更新版本 与 镜像自带版本 取较新的；镜像更新（docker compose pull）后旧的覆盖版本自动清理。
3. 启动 FastAPI 子进程并检查 /api/health；覆盖版本启动失败 → 标记坏版本，回退到上一个版本或镜像自带版本。
4. 子进程退出码 75 = 程序请求重启（装更新）；其他退出 → 3 秒后重启。监督进程自己不退出，所以不依赖 Docker 的 restart 策略。
收到 SIGTERM / SIGINT 时转发给子进程后退出（docker stop 正常）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

IMAGE_ROOT = Path(os.environ.get("LP_IMAGE_ROOT", "/app"))
DATA = Path(os.environ.get("LP_DATA_DIR", "/data"))
APPS = DATA / "app"
UPDATES = DATA / "updates"
PENDING = UPDATES / "pending.json"
ACTIVE = APPS / "active.json"
RESULT = UPDATES / "last_result.json"
RESTART_CODE = 75
PORT = int(os.environ.get("LP_PORT", "8000"))
HEALTH_TIMEOUT = float(os.environ.get("LP_HEALTH_TIMEOUT", "120"))
_child: subprocess.Popen | None = None
_stopping = False


def log(msg: str) -> None:
    print(f"[supervisor] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", str(v or ""))[:4]) or (0,)


def code_version(root: Path) -> str:
    m = re.search(r'__version__ = "(.+?)"', (root / "backend" / "app" / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "0"


def requirements_hash(root: Path) -> str:
    h = hashlib.sha256()
    for name in ("requirements.txt", "requirements-cluster.txt"):
        p = root / name
        h.update(name.encode() + b"\0" + (p.read_bytes().replace(b"\r\n", b"\n") if p.is_file() else b"") + b"\0")
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def result(ok: bool, message: str, **kw) -> None:
    write_json(RESULT, {"ok": ok, "message": message, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **kw})
    log(message)


def self_test(root: Path) -> str:
    env = dict(os.environ, PYTHONPATH=str(root), LP_SCHEDULER="0", LP_AUTO_SYNC="0", LP_WARM="0")
    r = subprocess.run([sys.executable, "-c", "import backend.app.main, backend.app; print(backend.app.__version__)"],
                       cwd=str(root), env=env, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError("导入自检失败：" + (r.stderr.strip().splitlines() or ["?"])[-1][:300])
    return r.stdout.strip()


def install_pending() -> None:
    pend = read_json(PENDING, None)
    if not pend:
        return
    PENDING.unlink(missing_ok=True)  # 不管成败只尝试一次，避免坏包反复重启
    pkg = Path(pend.get("package", ""))
    version = str(pend.get("version", ""))
    tmp = APPS / f".{version}.tmp"
    try:
        if not pkg.is_file():
            raise RuntimeError("更新包不见了")
        if sha256_file(pkg) != pend.get("sha256"):
            raise RuntimeError("更新包 sha256 不一致")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        with tarfile.open(pkg, "r:gz") as tf:
            for m in tf.getmembers():
                target = (tmp / m.name).resolve()
                if tmp.resolve() not in target.parents and target != tmp.resolve():
                    raise RuntimeError("更新包里有不安全的路径")
                if m.issym() or m.islnk():
                    raise RuntimeError("更新包里不允许链接")
            tf.extractall(tmp)
        manifest = read_json(tmp / "manifest.json", {})
        if manifest.get("version") != version or code_version(tmp) != version:
            raise RuntimeError("更新包版本号对不上")
        if manifest.get("requirements_sha256") != requirements_hash(IMAGE_ROOT):
            raise RuntimeError("新版本需要新的 Python 依赖，不能程序内安装，请执行 docker compose pull && docker compose up -d")
        got = self_test(tmp)
        if got != version:
            raise RuntimeError(f"自检版本是 {got}")
        dest = APPS / version
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        state = read_json(ACTIVE, {})
        prev = state.get("active")
        write_json(ACTIVE, {"active": version, "previous": prev if prev != version else state.get("previous"), "bad": state.get("bad", [])})
        result(True, f"已安装 v{version}，正在启动", version=version, phase="installed")
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        result(False, f"安装更新失败：{exc}", version=version, phase="install_failed")


def choose() -> tuple[Path, str, bool]:
    """返回 (代码目录, 版本, 是否覆盖版本)。"""
    image_v = code_version(IMAGE_ROOT)
    state = read_json(ACTIVE, {})
    bad = set(state.get("bad", []))
    for cand in (state.get("active"), state.get("previous")):
        if cand and cand not in bad and (APPS / cand / "backend").is_dir() and vtuple(cand) > vtuple(image_v):
            return APPS / cand, cand, True
    # 镜像自带版本更新（或覆盖版本都坏了）：清掉不再需要的覆盖版本
    if APPS.is_dir():
        for d in APPS.iterdir():
            if d.is_dir() and not d.name.startswith(".") and vtuple(d.name) <= vtuple(image_v):
                shutil.rmtree(d, ignore_errors=True)
                log(f"清理旧的程序内更新 v{d.name}（镜像已是 v{image_v}）")
        if state.get("active") and vtuple(state["active"]) <= vtuple(image_v):
            write_json(ACTIVE, {"active": None, "previous": None, "bad": sorted(bad)})
    return IMAGE_ROOT, image_v, False


def healthy(version: str, child: subprocess.Popen) -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=3) as r:
                if json.loads(r.read().decode()).get("version") == version:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def mark_bad(version: str) -> None:
    state = read_json(ACTIVE, {})
    state["bad"] = sorted(set(state.get("bad", [])) | {version})
    if state.get("active") == version:
        state["active"] = state.get("previous")
        state["previous"] = None
    write_json(ACTIVE, state)


def launch(root: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(root), LP_STATIC_DIR=str(root / "frontend" / "dist"), LP_SUPERVISED="1",
               LP_CODE_ROOT=str(root), LP_IMAGE_ROOT=str(IMAGE_ROOT))
    return subprocess.Popen([sys.executable, "-m", "backend.app.main"], cwd=str(root), env=env)


def _forward(signum, _frame):
    global _stopping
    _stopping = True
    if _child and _child.poll() is None:
        _child.send_signal(signum)


def main() -> int:
    global _child
    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)
    backoff = 3
    while not _stopping:
        install_pending()
        root, version, overlay = choose()
        log(f"启动 v{version}（{'程序内更新' if overlay else '镜像自带'}：{root}）")
        _child = launch(root)
        if not healthy(version, _child):
            if _stopping:
                break
            log(f"v{version} 没通过健康检查")
            if _child.poll() is None:
                _child.terminate()
                try:
                    _child.wait(20)
                except subprocess.TimeoutExpired:
                    _child.kill()
            if overlay:
                mark_bad(version)
                result(False, f"v{version} 启动失败，已回退", version=version, phase="rolled_back")
                continue
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        backoff = 3
        if overlay and read_json(RESULT, {}).get("phase") == "installed":
            result(True, f"更新完成：已运行 v{version}", version=version, phase="done")
        code = _child.wait()
        if _stopping:
            break
        log(f"子进程退出（{code}）{'，按请求重启' if code == RESTART_CODE else '，3 秒后重启'}")
        if code != RESTART_CODE:
            time.sleep(3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
