"""v0.5.3 Docker 程序内更新：更新包、监督进程安装 / 选择 / 回退 / 清理、依赖哈希、下载校验、重启后状态。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sup = _load("lp_supervisor_t", ROOT / "docker" / "supervisor.py")
pkgmod = _load("lp_pkg_t", ROOT / "scripts" / "build_app_package.py")


@pytest.fixture()
def dist_ready():
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        pytest.skip("前端没构建")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    image = tmp_path / "image"
    for name in ("requirements.txt", "requirements-cluster.txt"):
        (image / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, image / name)
    (image / "backend" / "app").mkdir(parents=True)
    (image / "backend" / "app" / "__init__.py").write_text('__version__ = "0.5.3"\n')
    data = tmp_path / "data"
    monkeypatch.setattr(sup, "IMAGE_ROOT", image)
    monkeypatch.setattr(sup, "DATA", data)
    monkeypatch.setattr(sup, "APPS", data / "app")
    monkeypatch.setattr(sup, "UPDATES", data / "updates")
    monkeypatch.setattr(sup, "PENDING", data / "updates" / "pending.json")
    monkeypatch.setattr(sup, "ACTIVE", data / "app" / "active.json")
    monkeypatch.setattr(sup, "RESULT", data / "updates" / "last_result.json")
    return image, data


def _pending(data: Path, pkg: Path, version: str, sha: str | None = None):
    (data / "updates").mkdir(parents=True, exist_ok=True)
    sup.write_json(sup.PENDING, {"version": version, "package": str(pkg), "sha256": sha or sup.sha256_file(pkg)})


def test_requirements_hash_matches_app_side():
    from backend.app.services import update_service

    assert sup.requirements_hash(ROOT) == update_service.requirements_hash(ROOT)


def test_package_contents(tmp_path, dist_ready):
    pkg = pkgmod.build(tmp_path, "9.9.9")
    import tarfile

    with tarfile.open(pkg) as tf:
        names = set(tf.getnames())
        manifest = json.loads(tf.extractfile("manifest.json").read())
        init = tf.extractfile("backend/app/__init__.py").read().decode()
    assert "frontend/dist/index.html" in names and "backend/app/main.py" in names and "docker/supervisor.py" not in names
    assert not any("/tests/" in n or "__pycache__" in n for n in names)
    assert manifest["version"] == "9.9.9" and '__version__ = "9.9.9"' in init
    assert manifest["requirements_sha256"] == sup.requirements_hash(ROOT)
    assert (tmp_path / (pkg.name + ".sha256")).read_text().split()[0] == hashlib.sha256(pkg.read_bytes()).hexdigest()


def test_install_choose_and_cleanup(env, tmp_path, dist_ready):
    image, data = env
    pkg = pkgmod.build(tmp_path / "out", "9.9.9")
    _pending(data, pkg, "9.9.9")
    sup.install_pending()
    res = json.loads(sup.RESULT.read_text())
    assert res["ok"] and res["phase"] == "installed", res
    assert not sup.PENDING.exists()
    root, version, overlay = sup.choose()
    assert overlay and version == "9.9.9" and root == data / "app" / "9.9.9"
    # 之后镜像更新到更高版本：镜像优先，旧覆盖版本被清理
    (image / "backend" / "app" / "__init__.py").write_text('__version__ = "10.0.0"\n')
    root, version, overlay = sup.choose()
    assert not overlay and version == "10.0.0" and not (data / "app" / "9.9.9").exists()


def test_install_rejects_bad_sha_and_new_deps(env, tmp_path, dist_ready):
    image, data = env
    pkg = pkgmod.build(tmp_path / "out", "9.9.9")
    _pending(data, pkg, "9.9.9", sha="0" * 64)
    sup.install_pending()
    assert "sha256" in json.loads(sup.RESULT.read_text())["message"]
    (image / "requirements.txt").write_text("somethingnew==1.0\n")
    _pending(data, pkg, "9.9.9")
    sup.install_pending()
    res = json.loads(sup.RESULT.read_text())
    assert res["ok"] is False and "依赖" in res["message"] and "docker compose pull" in res["message"]
    assert sup.choose()[2] is False  # 没装上，仍用镜像自带


def test_mark_bad_rolls_back(env):
    image, data = env
    for v in ("9.0.0", "9.1.0"):
        (data / "app" / v / "backend").mkdir(parents=True)
    sup.write_json(sup.ACTIVE, {"active": "9.1.0", "previous": "9.0.0", "bad": []})
    assert sup.choose()[1] == "9.1.0"
    sup.mark_bad("9.1.0")
    assert sup.choose()[1] == "9.0.0"
    sup.mark_bad("9.0.0")
    assert sup.choose()[2] is False  # 都坏了 → 镜像自带


# ---------- 程序这一侧 ----------
def test_prepare_docker_and_apply(tmp_path, monkeypatch, dist_ready):
    from backend.app.services import update_service as us

    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LP_HEADLESS", "1")
    monkeypatch.setenv("LP_SUPERVISED", "1")
    monkeypatch.setenv("LP_IMAGE_ROOT", str(ROOT))
    pkg = pkgmod.build(tmp_path / "out", "9.9.9")
    data = pkg.read_bytes()
    sha = (tmp_path / "out" / (pkg.name + ".sha256")).read_text()
    release = {"tag_name": "v9.9.9", "assets": [{"name": pkg.name, "browser_download_url": "https://dl.local/app.tgz"},
                                                {"name": pkg.name + ".sha256", "browser_download_url": "https://dl.local/app.tgz.sha256"}]}

    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=release)
        return httpx.Response(200, text=sha) if request.url.path.endswith(".sha256") else httpx.Response(200, content=data)

    us.set_http_client(httpx.Client(transport=httpx.MockTransport(handler)))
    us.STATE.update(phase="idle")
    try:
        info = us.check(force=True)
        assert info["mode"] == "docker" and info["docker_inapp"] and info["can_auto"] and info["app_url"]
        us.prepare_docker(info)
        assert us.status()["phase"] == "ready"
        exits = []
        monkeypatch.setattr(us.threading, "Timer", lambda t, fn: type("T", (), {"start": lambda self: exits.append(t)})())
        st = us.apply_docker()
        assert st["phase"] == "applying" and exits == [1.5]
        pend = json.loads((tmp_path / "data" / "updates" / "pending.json").read_text())
        assert pend["version"] == "9.9.9" and pend["sha256"] == hashlib.sha256(data).hexdigest()
        from backend.app.services import backups

        assert any("pre-update" in p.name for p in backups.list_backups())
        # 模拟重启后（内存状态丢失），监督进程写了失败结果 → 状态显示失败
        us.STATE.update(phase="idle")
        (tmp_path / "data" / "updates" / "last_result.json").write_text(json.dumps({"ok": False, "message": "v9.9.9 启动失败，已回退", "version": "9.9.9"}))
        assert us.status()["phase"] == "error" and "回退" in us.status()["message"]
    finally:
        us.set_http_client(None)
        us.STATE.update(phase="idle", message="", error="")


def test_prepare_docker_refuses_new_dependencies(tmp_path, monkeypatch, dist_ready):
    from backend.app.services import update_service as us

    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path / "data"))
    image = tmp_path / "image"
    image.mkdir()
    (image / "requirements.txt").write_text("other==1\n")
    monkeypatch.setenv("LP_IMAGE_ROOT", str(image))
    pkg = pkgmod.build(tmp_path / "out", "9.9.9")
    sha = (tmp_path / "out" / (pkg.name + ".sha256")).read_text()
    us.set_http_client(httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=sha) if r.url.path.endswith(".sha256") else httpx.Response(200, content=pkg.read_bytes()))))
    try:
        with pytest.raises(RuntimeError, match="docker compose pull"):
            us.prepare_docker({"latest": "9.9.9", "app_url": "https://x/a", "app_sha256_url": "https://x/a.sha256"})
    finally:
        us.set_http_client(None)
        us.STATE.update(phase="idle", message="", error="")


def test_apply_requires_confirm_in_docker(client, monkeypatch):
    monkeypatch.setenv("LP_HEADLESS", "1")
    from backend.app.core import auth

    monkeypatch.setattr(auth, "required", lambda: False)
    assert client.post("/api/update/apply", json={}).status_code == 400
    assert client.post("/api/update/apply", json={"confirm": True}).status_code == 409  # 还没下载
