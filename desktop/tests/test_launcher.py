from __future__ import annotations

import json
import socket
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desktop.launcher import (
    WEBVIEW2_MISSING_MESSAGE,
    ControlServer,
    ExportApi,
    SingleInstance,
    bind_server_socket,
    default_save_directory,
    enable_webview_downloads,
    explain_webview_unavailable,
    load_app,
    main,
    resolve_data_dir,
    resolve_static_dir,
    sanitize_export_filename,
    save_text_as,
    signal_existing_instance,
    version_at_least,
    window_close_action,
)
from desktop.stub_app import create_app


def test_version_at_least():
    assert version_at_least("120.0.2210.91")
    assert version_at_least("86.0.622.0")
    assert not version_at_least("86.0.621.0")
    assert not version_at_least("0.0.0.0")
    assert not version_at_least("0")
    assert not version_at_least("")


def test_data_dir_env_and_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path / "custom"))
    assert resolve_data_dir() == tmp_path / "custom"

    monkeypatch.delenv("LP_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert resolve_data_dir() == Path.home() / ".limit_pattern"

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/me/AppData/Local")
    assert resolve_data_dir() == Path("C:/Users/me/AppData/Local/LimitPattern")


def test_static_dir_order(monkeypatch, tmp_path):
    frontend = tmp_path / "frontend" / "dist"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("frontend", encoding="utf-8")
    monkeypatch.delenv("LP_STATIC_DIR", raising=False)
    monkeypatch.setattr("desktop.launcher.is_frozen", lambda: False)
    monkeypatch.setattr("desktop.launcher.project_root", lambda: tmp_path)
    path, kind = resolve_static_dir()
    assert kind == "frontend"
    assert path == frontend

    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv("LP_STATIC_DIR", str(override))
    path, kind = resolve_static_dir()
    assert kind == "env"
    assert path == override

    monkeypatch.delenv("LP_STATIC_DIR", raising=False)
    monkeypatch.setattr("desktop.launcher.project_root", lambda: tmp_path / "empty")
    path, kind = resolve_static_dir()
    assert kind == "stub"
    assert (path / "index.html").is_file()


def test_bind_skips_busy_port():
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    busy = held.getsockname()[1]
    try:
        sock, port = bind_server_socket("127.0.0.1", busy, attempts=5)
        try:
            assert port != busy
            assert port > busy
        finally:
            sock.close()
    finally:
        held.close()


def test_stub_routes_match_contract():
    static = Path(__file__).resolve().parents[1] / "stub_static"
    with TestClient(create_app(static)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        assert body["data_date"] is None
        assert str(body["version"]).endswith("-stub")

        missing = client.get("/api/settings")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "stub 未实现该接口"

        page = client.get("/")
        assert page.status_code == 200
        assert "相似度不是涨停概率" in page.text
        assert "三日涨停形态统计" in page.text

        fallback = client.get("/similarity/000001")
        assert fallback.status_code == 200
        assert "相似度不是涨停概率" in fallback.text


def test_prefers_backend_and_can_force_stub(monkeypatch, tmp_path):
    backend = types.ModuleType("backend")
    app_pkg = types.ModuleType("backend.app")
    main_mod = types.ModuleType("backend.app.main")
    sentinel = object()
    main_mod.app = sentinel
    monkeypatch.setitem(sys.modules, "backend", backend)
    monkeypatch.setitem(sys.modules, "backend.app", app_pkg)
    monkeypatch.setitem(sys.modules, "backend.app.main", main_mod)
    monkeypatch.delenv("LP_USE_STUB", raising=False)
    app, source = load_app(tmp_path)
    assert source == "backend"
    assert app is sentinel

    monkeypatch.setenv("LP_USE_STUB", "yes")
    app, source = load_app(tmp_path)
    assert source == "stub"
    assert app is not sentinel


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_stub_switch_values(monkeypatch, tmp_path, value):
    monkeypatch.setenv("LP_USE_STUB", value)
    _app, source = load_app(tmp_path)
    assert source == "stub"


def test_single_instance_lock(tmp_path):
    first = SingleInstance(tmp_path)
    assert first.try_acquire()
    second = SingleInstance(tmp_path)
    try:
        assert not second.try_acquire()
    finally:
        first.release()
    third = SingleInstance(tmp_path)
    assert third.try_acquire()
    third.release()


def test_control_server_show(tmp_path):
    seen: list[str] = []
    server = ControlServer(tmp_path, lambda: seen.append("show"))
    server.start(8765, "http://127.0.0.1:8765/")
    try:
        assert signal_existing_instance(tmp_path)
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen == ["show"]
        info = json.loads((tmp_path / "instance.json").read_text(encoding="utf-8"))
        assert info["port"] == 8765
        assert info["control_port"] == server.port
    finally:
        server.stop()


def test_signal_without_instance_file(tmp_path):
    assert signal_existing_instance(tmp_path) is False


def test_window_close_action():
    assert window_close_action(False, True) == "hide"
    assert window_close_action(True, True) == "destroy"
    assert window_close_action(False, False) == "destroy"


def test_webview_fallback_message(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert explain_webview_unavailable(False) is None
    assert explain_webview_unavailable(True) == ""

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("desktop.launcher.webview2_installed", lambda: False)
    assert explain_webview_unavailable(False) == WEBVIEW2_MISSING_MESSAGE
    monkeypatch.setattr("desktop.launcher.webview2_installed", lambda: True)
    assert explain_webview_unavailable(False) is None


def test_smoke_uses_stub_and_ignores_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LP_USE_STUB", "1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    assert main(["--smoke"]) == 0
    text = (tmp_path / "logs" / "launcher.log").read_text(encoding="utf-8")
    assert "应用来源=stub" in text
    assert "健康检查通过" in text
    assert "首页检查通过" in text
    assert '"status": "ok"' in text


def test_export_filename_and_default_directory(tmp_path):
    assert sanitize_export_filename(r"..\pool") == "pool.txt"
    assert sanitize_export_filename("报告.txt") == "报告.txt"
    assert sanitize_export_filename("") == "export.txt"
    assert sanitize_export_filename("a<b>.txt") == "a_b_.txt"

    (tmp_path / "Downloads").mkdir()
    assert default_save_directory(tmp_path) == tmp_path / "Downloads"
    (tmp_path / "Desktop").mkdir()
    assert default_save_directory(tmp_path) == tmp_path / "Desktop"


def test_save_text_as_writes_utf8_bom(monkeypatch, tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr("desktop.launcher.default_save_directory", lambda home=None: desktop)
    target = desktop / "涨停.txt"

    def ask(directory, name):
        assert directory == desktop
        assert name == "涨停.txt"
        return str(target)

    saved = save_text_as("涨停", "你好\n", ask)
    assert saved == str(target)
    blob = target.read_bytes()
    assert blob.startswith(b"\xef\xbb\xbf")
    assert blob.decode("utf-8-sig") == "你好\n"
    assert save_text_as("涨停.txt", "x", lambda _directory, _name: None) is None


def test_export_api_save_file_uses_save_dialog(monkeypatch, tmp_path):
    import types

    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr("desktop.launcher.default_save_directory", lambda home=None: desktop)
    fake_webview = types.ModuleType("webview")

    class _FileDialog:
        SAVE = 30

    fake_webview.FileDialog = _FileDialog
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    class FakeWindow:
        def __init__(self):
            self.kwargs = None

        def create_file_dialog(self, dialog_type, directory="", allow_multiple=False, save_filename="", file_types=()):
            self.kwargs = {
                "dialog_type": dialog_type,
                "directory": directory,
                "save_filename": save_filename,
                "file_types": file_types,
            }
            return (str(desktop / save_filename),)

    window = FakeWindow()
    api = ExportApi()
    api.window = window
    saved = api.save_file("pool", "甲")
    assert window.kwargs["dialog_type"] == 30
    assert window.kwargs["directory"] == str(desktop)
    assert window.kwargs["save_filename"] == "pool.txt"
    assert saved == str(desktop / "pool.txt")
    assert (desktop / "pool.txt").read_bytes().startswith(b"\xef\xbb\xbf")


def test_enable_webview_downloads():
    settings = {"ALLOW_DOWNLOADS": False}
    assert enable_webview_downloads(settings)["ALLOW_DOWNLOADS"] is True


def test_smoke_reports_existing_instance(monkeypatch, tmp_path):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LP_USE_STUB", "1")
    holder = SingleInstance(tmp_path)
    assert holder.try_acquire()
    try:
        assert main(["--smoke"]) == 2
    finally:
        holder.release()
    text = (tmp_path / "logs" / "launcher.log").read_text(encoding="utf-8")
    assert "已有实例" in text
