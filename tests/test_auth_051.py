"""v0.5.1 账号登录：用户名 + 密码、记住我长期 cookie、改密码 / 退出所有设备失效、锁定、0.5.0 迁移、环境变量、免登录路由。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app.core import auth


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("LP_PASSWORD", raising=False)
    monkeypatch.delenv("LP_USER", raising=False)
    auth.reset_limits()
    yield
    auth.reset_limits()


def login(client, user="boss", pw="s3cret-pw", remember=True):
    return client.post("/api/auth/login", json={"username": user, "password": pw, "remember": remember})


def setup(client, monkeypatch, user="boss", pw="s3cret-pw"):
    monkeypatch.setenv("LP_HEADLESS", "1")
    st = client.get("/api/auth/status").json()
    assert st["required"] and st["setup_needed"] and not st["logged_in"]
    r = client.post("/api/auth/setup", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r


def test_desktop_needs_no_login(client):
    st = client.get("/api/auth/status").json()
    assert st["required"] is False and st["logged_in"] is True
    assert client.get("/api/settings").status_code == 200


def test_setup_then_login_success_and_failure(client, monkeypatch):
    assert client.get("/api/settings").status_code == 200
    monkeypatch.setenv("LP_HEADLESS", "1")
    assert client.get("/api/settings").status_code == 401  # 无账号也挡
    monkeypatch.delenv("LP_HEADLESS")
    r = setup(client, monkeypatch)
    cookie = r.cookies.get(auth.COOKIE) or client.cookies.get(auth.COOKIE)
    assert cookie and "Max-Age" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"] and "samesite=lax" in r.headers["set-cookie"].lower()
    assert client.post("/api/auth/setup", json={"username": "x", "password": "123456"}).status_code == 409
    assert client.get("/api/settings").status_code == 200
    client.cookies.clear()
    assert client.get("/api/settings").status_code == 401
    assert login(client, pw="wrong").status_code == 401
    assert login(client, user="other").status_code == 401
    r = login(client, user="BOSS")  # 用户名不区分大小写
    assert r.status_code == 200 and client.get("/api/settings").status_code == 200
    raw = (auth.data_dir() / "auth.json").read_text(encoding="utf-8")
    assert "s3cret-pw" not in raw and "pbkdf2$" in raw
    # 不勾记住我 → 会话 cookie（无 Max-Age）
    client.cookies.clear()
    r = login(client, remember=False)
    assert "Max-Age" not in r.headers["set-cookie"]


def test_cookie_survives_restart(client, monkeypatch):
    setup(client, monkeypatch)
    token = client.cookies.get(auth.COOKIE)
    from backend.app.main import create_app

    with TestClient(create_app()) as again:  # 同一数据目录重新启动
        again.cookies.set(auth.COOKIE, token)
        assert again.get("/api/settings").status_code == 200
        assert again.get("/api/auth/status").json()["username"] == "boss"


def test_password_change_invalidates_old_cookies(client, monkeypatch):
    setup(client, monkeypatch)
    old = client.cookies.get(auth.COOKIE)
    other = TestClient(client.app)
    other.cookies.set(auth.COOKIE, old)
    assert other.get("/api/settings").status_code == 200
    assert client.post("/api/auth/change", json={"current_password": "bad", "new_password": "newpass1"}).status_code == 403
    assert client.post("/api/auth/change", json={"current_password": "s3cret-pw", "new_password": "newpass1"}).status_code == 200
    assert other.get("/api/settings").status_code == 401  # 其他设备失效
    assert client.get("/api/settings").status_code == 200  # 本机换发了新 cookie
    client.cookies.clear()
    assert login(client).status_code == 401 and login(client, pw="newpass1").status_code == 200
    # 改用户名也让旧 cookie 失效
    tok = client.cookies.get(auth.COOKIE)
    assert client.post("/api/auth/change", json={"current_password": "newpass1", "username": "chief"}).status_code == 200
    other.cookies.set(auth.COOKIE, tok)
    assert other.get("/api/settings").status_code == 401
    assert login(client, user="chief", pw="newpass1").status_code == 200


def test_logout_and_logout_all(client, monkeypatch):
    setup(client, monkeypatch)
    a = client.cookies.get(auth.COOKIE)
    login(client)
    b = client.cookies.get(auth.COOKIE)
    assert a != b and len(client.get("/api/auth/sessions").json()["items"]) == 2
    client.post("/api/auth/logout")
    t = TestClient(client.app)
    t.cookies.set(auth.COOKIE, b)
    assert t.get("/api/settings").status_code == 401  # 退出的那个失效
    t.cookies.set(auth.COOKIE, a)
    assert t.get("/api/settings").status_code == 200  # 另一台还在
    t.post("/api/auth/logout-all")
    t.cookies.set(auth.COOKIE, a)
    assert t.get("/api/settings").status_code == 401


def test_lockout_after_five_failures(client, monkeypatch):
    setup(client, monkeypatch)
    client.cookies.clear()
    codes = [login(client, pw="nope").status_code for _ in range(5)]
    assert codes[:4] == [401] * 4 and codes[4] == 429
    r = login(client)  # 正确密码也被挡
    assert r.status_code == 429 and "分钟" in r.json()["detail"]
    monkeypatch.setattr(auth.time, "time", lambda real=auth.time.time: real() + 301)
    assert login(client).status_code == 200


def test_migrate_050_password(client, monkeypatch):
    from backend.app.core.config import settings_path, write_settings_file, read_settings_file

    raw = read_settings_file()
    raw["access_password"] = "pbkdf2$" + __import__("base64").b64encode(b"0" * 16).decode() + "$" + __import__("base64").b64encode(
        __import__("hashlib").pbkdf2_hmac("sha256", b"old-pass", b"0" * 16, 200_000)).decode()
    write_settings_file(raw)
    monkeypatch.setenv("LP_HEADLESS", "1")
    st = client.get("/api/auth/status").json()
    assert st["setup_needed"] is False
    assert login(client, user="admin", pw="old-pass").status_code == 200
    assert json.loads(settings_path().read_text(encoding="utf-8"))["access_password"] == ""


def test_env_seed(client, monkeypatch):
    monkeypatch.setenv("LP_PASSWORD", "env-pass")
    assert client.get("/api/settings").status_code == 401
    assert login(client, user="admin", pw="env-pass").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.post("/api/auth/change", json={"current_password": "env-pass", "new_password": "xxxxxx"}).status_code == 422
    client.cookies.clear()
    monkeypatch.setenv("LP_USER", "ops")
    assert login(client, user="admin", pw="env-pass").status_code == 401
    assert login(client, user="ops", pw="env-pass").status_code == 200


def test_exempt_routes(client, monkeypatch):
    monkeypatch.setenv("LP_HEADLESS", "1")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/api/wecom/callback").status_code == 404  # 不是 401：回调免登录（没开启所以 404）
    for path in ("/api/settings", "/api/auth/sessions", "/api/watchlist", "/api/update/check"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/auth/logout-all").status_code == 401


def test_backup_keeps_account_and_key(client, monkeypatch):
    setup(client, monkeypatch)
    from backend.app.services import backups

    b = backups.make_backup("t")
    assert (b / "auth.json").is_file() and (b / ".session_secret").is_file()
