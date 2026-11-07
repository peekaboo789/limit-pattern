"""v0.5.1：文本格式（个人微信能显示）、分段、格式设置与迁移、底部菜单、菜单点击、在线更新、升级备份、程序替换。"""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
import hashlib

import httpx
import pytest

from backend.app.services.wecom import client as wc
from backend.app.services.wecom.crypto import WXBizMsgCrypt, extract
from tests.test_wecom import AES, CORP, TOKEN, MockWeCom, cfg, write_settings


@pytest.fixture()
def mock(monkeypatch, tmp_path):
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(wc, "MIN_INTERVAL", 0)
    m = MockWeCom()
    orig = m.handler
    m.menus = []

    def handler(request):
        if request.url.path == "/cgi-bin/menu/create":
            code = m.send_codes.pop(0) if m.send_codes else 0
            if code == 0:
                m.menus.append((request.url.params["agentid"], json.loads(request.content)))
            return httpx.Response(200, json={"errcode": code, "errmsg": "mock"})
        return orig(request)

    wc.set_http_client(httpx.Client(transport=httpx.MockTransport(handler)))
    yield m
    wc.set_http_client(None)


# ---------- 文本格式与分段 ----------
def test_split_text_bytes_and_pages():
    lines = [f"{i:>2}. 贵州茅台(600519) 综合 88.{i}" for i in range(200)]
    parts = wc.split_text("\n".join(lines))
    assert len(parts) > 1 and all(len(p.encode()) <= 2048 for p in parts)
    assert parts[0].endswith(f"（1/{len(parts)}）") and "199. 贵州茅台" in parts[-1]
    joined = "\n".join(p.rsplit("\n", 1)[0] for p in parts)
    assert joined == "\n".join(lines)  # 按行切，不丢不重
    one = wc.split_text("短消息")
    assert one == ["短消息"]
    huge = wc.split_text("涨" * 3000)  # 单行超长硬切
    assert all(len(p.encode()) <= 2048 for p in huge)


def test_render_formats():
    t = wc.render("📊 标题", "a\nb", "text")
    assert t == [("text", {"content": "📊 标题\na\nb"})]
    card = wc.render("标题", "a\nb", "textcard", "")[0]
    assert card[0] == "textcard" and card[1]["description"] == "a<br>b" and card[1]["url"].startswith("https://")
    assert wc.render("T", "x", "markdown")[0] == ("markdown", {"content": "**T**\nx"})


def test_default_is_text_and_webhook_text(mock):
    c = cfg()
    assert c["format"] == "text"
    wc.deliver("标题", "内容", cfg=c)
    assert mock.sent[-1]["msgtype"] == "text" and "markdown" not in mock.sent[-1]
    hook = cfg(wecom_secret="", wecom_webhook="https://mock.local/cgi-bin/webhook/send?key=fake")
    wc.deliver("标题", "内容", cfg=hook)
    assert mock.hooks[-1]["msgtype"] == "text"


def test_long_reply_is_split(mock):
    wc.deliver("长", "\n".join(f"行{i} " + "数据" * 20 for i in range(200)), cfg=cfg())
    assert len(mock.sent) > 1 and all(m["msgtype"] == "text" and len(m["text"]["content"].encode()) <= 2048 for m in mock.sent)


def test_format_setting_and_migration(client):
    view = client.get("/api/settings").json()
    assert view["wecom_msg_format"] == "text"
    assert client.put("/api/settings", json={"wecom_msg_format": "textcard"}).json()["wecom_msg_format"] == "textcard"
    assert client.put("/api/settings", json={"wecom_msg_format": "html"}).status_code == 422
    # 0.5.0 的设置文件里没有格式字段 → 迁移为文本
    from backend.app.core.config import settings_path
    from backend.app.services import backups

    raw = json.loads(settings_path().read_text(encoding="utf-8"))
    raw.pop("wecom_msg_format")
    settings_path().write_text(json.dumps(raw), encoding="utf-8")
    assert backups.migrate_v051() is True
    assert json.loads(settings_path().read_text(encoding="utf-8"))["wecom_msg_format"] == "text"
    assert backups.migrate_v051() is False


def test_settings_cache_key_includes_size(client):
    """同一 mtime 下两次重写不同内容，缓存不能只靠 (path, mtime) 返回旧 payload。"""
    from backend.app.core import config
    from backend.app.core.config import settings_path

    client.put("/api/settings", json={"wecom_corpid": "wwcache"})
    path = settings_path()
    first = config._load_settings_payload(path)
    assert first["wecom_corpid"] == "wwcache"
    # 模拟同一次 stat（mtime_ns 相同）但文件内容已变长：只改 size 就能让缓存失效。
    st = path.stat()
    config._SETTINGS_CACHE.update(path=str(path), mtime=st.st_mtime_ns, size=st.st_size, payload=first)
    bigger = dict(first)
    bigger["wecom_corpid"] = "wwcache-longer"
    path.write_text(json.dumps(bigger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # 锁死 mtime，只剩 size 不同
    reloaded = config._load_settings_payload(path)
    assert reloaded["wecom_corpid"] == "wwcache-longer"


# ---------- 菜单 ----------
def test_menu_json_limits():
    from backend.app.services.wecom import commands, menu

    m = menu.menu_json()["button"]
    assert len(m) == 3 and all(1 <= len(b["sub_button"]) <= 5 for b in m)
    assert all(len(b["name"].encode()) <= 16 for b in m)
    for b in m:
        for sub in b["sub_button"]:
            assert sub["type"] == "click" and len(sub["name"].encode()) <= 40 and sub["key"] in commands.MENU
    assert menu.KEYS <= set(commands.MENU)  # 旧菜单的 key（添加说明等）仍能用


def test_menu_create_against_mock(client, mock):
    write_settings(client)
    r = client.post("/api/wecom/menu").json()
    assert r["ok"] and mock.menus[-1][0] == "1000002" and mock.menus[-1][1]["button"][0]["name"] == "榜单"
    mock.send_codes = [60011]
    r = client.post("/api/wecom/menu").json()
    assert r["ok"] is False and r["errcode"] == 60011 and "权限" in r["message"]


def _post(client, xml):
    c = WXBizMsgCrypt(TOKEN, AES, CORP)
    ts, nonce = str(int(time.time())), "n2"
    out = c.encrypt_msg(xml, nonce, ts)
    return client.post("/api/wecom/callback", params={"msg_signature": extract(out, "MsgSignature"), "timestamp": ts, "nonce": nonce}, content=out)


def _click(user, key):
    return (f"<xml><ToUserName>{CORP}</ToUserName><FromUserName>{user}</FromUserName><MsgType>event</MsgType>"
            f"<Event>click</Event><EventKey>{key}</EventKey><AgentID>1000002</AgentID></xml>")


def test_click_events_and_whitelist(client, mock, monkeypatch):
    write_settings(client)
    import backend.app.api.wecom_routes as routes

    got = []
    monkeypatch.setattr(routes, "_reply_async", lambda cfg, info: got.append(info))
    assert _post(client, _click("zhangsan", "WL_QUOTE")).text == "success"
    assert got[-1] == {"user": "zhangsan", "cmd": "自选", "arg": ""}
    _post(client, _click("zhangsan", "STATUS"))
    assert got[-1]["cmd"] == "状态"
    _post(client, _click("wangwu", "CAPTURE"))
    assert got[-1]["cmd"] is None and "白名单" in got[-1]["reply"][1]
    n = len(got)
    _post(client, _click("zhangsan", "UNKNOWN_KEY"))
    _post(client, f"<xml><FromUserName>zhangsan</FromUserName><MsgType>event</MsgType><Event>enter_agent</Event></xml>")
    assert len(got) == n  # 未知菜单 / 进入应用事件不回复


def test_click_end_to_end_reply_as_text(client, mock):
    write_settings(client)
    _post(client, _click("zhangsan", "WL_ADD_HELP"))
    for _ in range(50):
        if mock.sent:
            break
        time.sleep(0.05)
    msg = mock.sent[-1]
    assert msg["touser"] == "zhangsan" and msg["msgtype"] == "text" and "加自选" in msg["text"]["content"]


def test_long_action_replies_started_then_result():
    from backend.app.services.wecom import commands

    out = []
    first = commands._later(lambda t, b: out.append((t, b)), lambda: ("✅ 完成", "结果"), "已开始做")
    assert first == ("⏳ 已开始", "已开始做")
    for _ in range(50):
        if out:
            break
        time.sleep(0.02)
    assert out == [("✅ 完成", "结果")]


def test_menu_commands_run(client):
    from backend.app.services import watchlist_service as ws
    from backend.app.services.wecom import commands

    ws.add([{"code": "600519", "name": "贵州茅台", "note": "白酒"}])
    title, body = commands.run("导出自选")
    assert "1只" in title and "600519 贵州茅台 # 白酒" in body
    assert "加自选 代码/名称" in commands.run("添加说明")[1]
    title, body = commands.run("状态")
    assert "版本：v" in body and "数据日期" in body


# ---------- 在线更新 ----------
RELEASE = {"tag_name": "v9.9.9", "body": "- 新功能", "published_at": "2026-11-01T00:00:00Z", "html_url": "https://github.com/x/releases/tag/v9.9.9",
           "assets": [{"name": "LimitPattern-cluster-win64.zip", "browser_download_url": "https://dl.local/a.zip"},
                      {"name": "LimitPattern-cluster-win64.zip.sha256", "browser_download_url": "https://dl.local/a.zip.sha256"}]}


def test_version_compare_and_parse():
    from backend.app.services import update_service as us

    assert us.is_newer("0.5.1", "0.5.0") and us.is_newer("v0.10.0", "0.9.9") and not us.is_newer("0.5.0", "0.5.0")
    info = us.parse_release(RELEASE)
    assert info["latest"] == "9.9.9" and info["zip_url"].endswith("a.zip") and info["sha256_url"].endswith(".sha256")
    assert us.expected_sha("ABCDEF" * 10 + "abcd  LimitPattern.zip") == ("abcdef" * 10 + "abcd")


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("LimitPattern/LimitPattern.exe", b"new-exe")
        zf.writestr("LimitPattern/_internal/lib.txt", b"lib")
    return buf.getvalue()


def test_check_download_verify(monkeypatch, tmp_path):
    from backend.app.services import update_service as us

    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path))
    data = _zip_bytes()
    sha = {"v": hashlib.sha256(data).hexdigest()}

    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=RELEASE)
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{sha['v']}  LimitPattern-cluster-win64.zip\n")
        return httpx.Response(200, content=data)

    us.set_http_client(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        info = us.check(force=True)
        assert info["newer"] and info["latest"] == "9.9.9" and info["mode"] in ("source", "docker")
        program = us.prepare(info)
        assert (program / "LimitPattern.exe").read_bytes() == b"new-exe" and us.status()["phase"] == "ready"
        sha["v"] = "0" * 64
        with pytest.raises(RuntimeError, match="sha256"):
            us.prepare(info)
        title, body = us.wecom_text(dict(info, mode="docker"))
        assert "docker compose pull" in body and "9.9.9" in body
    finally:
        us.set_http_client(None)


def test_check_falls_back_to_branch_version(monkeypatch, tmp_path):
    from backend.app.services import update_service as us

    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(404, json={})
        return httpx.Response(200, text='__version__ = "0.0.1"\n')

    us.set_http_client(httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        info = us.check(force=True)
        assert info["latest"] == "0.0.1" and info["newer"] is False
    finally:
        us.set_http_client(None)


def test_update_api_routes(client):
    r = client.get("/api/update/status").json()
    assert r["phase"] in ("idle", "ready", "error") and "program" not in r
    assert client.post("/api/update/apply").status_code == 409  # 非桌面版 / 没下载


def test_swap_install_replaces_and_rolls_back(tmp_path):
    from backend.app.services.update_service import swap_install

    target, source = tmp_path / "LimitPattern", tmp_path / "new"
    (target / "_internal").mkdir(parents=True)
    (target / "LimitPattern.exe").write_text("old")
    (source / "_internal").mkdir(parents=True)
    (source / "LimitPattern.exe").write_text("new")
    old = swap_install(target, source)
    assert (target / "LimitPattern.exe").read_text() == "new" and (old / "LimitPattern.exe").read_text() == "old"

    import shutil

    def broken(*a, **k):
        raise OSError("disk full")

    real = shutil.copytree
    shutil.copytree = broken
    try:
        with pytest.raises(OSError):
            swap_install(target, source)
    finally:
        shutil.copytree = real
    assert (target / "LimitPattern.exe").read_text() == "new"  # 失败还原


def test_desktop_updater_waits_and_swaps(tmp_path):
    from desktop.updater import apply_update

    target, source = tmp_path / "app", tmp_path / "src"
    target.mkdir()
    (target / "LimitPattern.exe").write_text("old")
    source.mkdir()
    (source / "LimitPattern.exe").write_text("new")
    assert apply_update(target, source, pid=0, launch=False) == 0
    assert (target / "LimitPattern.exe").read_text() == "new"


# ---------- 升级备份 ----------
def test_backup_on_version_change_keeps_three(client, monkeypatch):
    from backend.app.core.config import data_dir
    from backend.app.services import backups
    from backend.app.services import watchlist_service as ws

    ws.add([{"code": "600519", "name": "贵州茅台"}])
    client.put("/api/settings", json={"wecom_corpid": "wwkeep"})
    (data_dir() / backups.MARKER).write_text("0.4.0")
    res = backups.on_start()
    assert res["changed"] and res["previous"] == "0.4.0" and res["backup"]
    from pathlib import Path

    b = Path(res["backup"])
    assert json.loads((b / "settings.json").read_text(encoding="utf-8"))["wecom_corpid"] == "wwkeep"
    assert (b / "watchlist.json").is_file() and (b / "lp.db").stat().st_size > 0
    assert backups.on_start()["changed"] is False  # 同版本再启动不备份
    for i in range(5):
        backups.make_backup(f"t{i}")
    assert len(backups.list_backups()) == 3
    # 数据原样保留
    assert ws.load()[0]["code"] == "600519" and client.get("/api/settings").json()["wecom_corpid"] == "wwkeep"
