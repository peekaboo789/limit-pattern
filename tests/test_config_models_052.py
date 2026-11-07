"""v0.5.2：配置导出 / 导入（默认不含密钥、预览、备份、密钥只在有值时覆盖、版本校验）与多个 AI 模型一键切换。"""

from __future__ import annotations

import json
from urllib.parse import unquote

import httpx
import pytest

KEY_A, KEY_B = "sk-aaaa-fake-0001", "sk-bbbb-fake-0002"


def _seed(client):
    r = client.put("/api/settings", json={"ai_base_url": "https://a.example/v1", "ai_model": "model-a", "ai_api_key": KEY_A,
                                          "wecom_corpid": "wwexport", "wecom_secret": "wecom-secret-fake", "watchlist_alerts": False})
    assert r.status_code == 200, r.text


# ---------- 多模型 ----------
def test_migrates_single_config_into_first_profile(client):
    _seed(client)
    pub = client.get("/api/ai/profiles").json()
    assert len(pub["items"]) == 1 and pub["items"][0]["model"] == "model-a" and pub["items"][0]["api_key_set"] and pub["items"][0]["active"]
    assert KEY_A not in json.dumps(pub)


def test_add_switch_edit_delete(client):
    _seed(client)
    first = client.get("/api/ai/profiles").json()["active"]
    pub = client.post("/api/ai/profiles", json={"name": "DeepSeek", "base_url": "https://b.example/v1", "model": "model-b", "api_key": KEY_B,
                                                "temperature": 0.7, "max_tokens": 4096}).json()
    second = next(p for p in pub["items"] if p["name"] == "DeepSeek")
    assert pub["active"] == first and not second["active"] and KEY_B not in json.dumps(pub)
    pub = client.post(f"/api/ai/profiles/{second['id']}/activate").json()
    assert pub["active"] == second["id"] and pub["active_name"] == "DeepSeek"
    from backend.app.core.config import read_settings_file

    raw = read_settings_file()
    assert raw["ai_model"] == "model-b" and raw["ai_api_key"] == KEY_B and raw["ai_temperature"] == 0.7 and raw["ai_max_tokens"] == 4096
    s = client.get("/api/settings").json()
    assert s["ai_model"] == "model-b" and KEY_B not in json.dumps(s)
    # 编辑：空 key 保留原 key
    client.put(f"/api/ai/profiles/{second['id']}", json={"model": "model-b2", "api_key": ""})
    raw = read_settings_file()
    assert raw["ai_model"] == "model-b2" and raw["ai_api_key"] == KEY_B
    assert client.put(f"/api/ai/profiles/{second['id']}", json={"temperature": 5}).status_code == 422
    assert client.put(f"/api/ai/profiles/{second['id']}", json={"base_url": "ftp://x"}).status_code == 422
    assert client.post("/api/ai/profiles/nope/activate").status_code == 404
    # 切回
    client.post(f"/api/ai/profiles/{first}/activate")
    assert read_settings_file()["ai_api_key"] == KEY_A
    # 删除当前 → 自动切到剩下的
    pub = client.delete(f"/api/ai/profiles/{first}").json()
    assert pub["active"] == second["id"] and read_settings_file()["ai_model"] == "model-b2"
    # 旧表单改 ai_model 也同步到当前配置
    client.put("/api/settings", json={"ai_model": "model-c"})
    assert client.get("/api/ai/profiles").json()["items"][0]["model"] == "model-c"


def test_profile_test_uses_its_own_key(client, monkeypatch):
    _seed(client)
    pub = client.post("/api/ai/profiles", json={"name": "B", "base_url": "https://b.example/v1", "model": "model-b", "api_key": KEY_B}).json()
    pid = next(p["id"] for p in pub["items"] if p["name"] == "B")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "连接成功"}}]})

    from backend.app.core import ai_client

    monkeypatch.setattr(ai_client, "_TRANSPORT", httpx.MockTransport(handler), raising=False)
    r = client.post("/api/ai/profiles/test", json={"id": pid}).json()
    assert r["ok"], r
    assert seen["auth"] == f"Bearer {KEY_B}" and seen["body"]["model"] == "model-b"
    assert KEY_B not in json.dumps(r)


def test_temperature_and_max_tokens_reach_request():
    from backend.app.core import ai_client

    assert ai_client._temperature({"ai_temperature": 0.9}) == 0.9 and ai_client._temperature({}) == 0.2
    assert ai_client._int_or_none("1024") == 1024 and ai_client._int_or_none(None) is None


# ---------- 导出 ----------
def test_export_excludes_secrets_by_default(client):
    _seed(client)
    r = client.get("/api/config/export")
    assert r.status_code == 200 and "filename*=UTF-8''" in r.headers["content-disposition"]
    assert "配置" in unquote(r.headers["content-disposition"])
    text = r.text
    doc = json.loads(text)
    assert doc["format"] == "limit-pattern-config" and doc["version"] == 1 and doc["includes_secrets"] is False
    assert KEY_A not in text and "wecom-secret-fake" not in text and "pbkdf2" not in text
    assert doc["settings"]["wecom_corpid"] == "wwexport" and doc["settings"]["watchlist_alerts"] is False
    assert "api_key" not in doc["settings"]["ai_profiles"][0] and "watchlist" not in doc


def test_export_with_secrets_and_watchlist(client, monkeypatch):
    _seed(client)
    from backend.app.services import watchlist_service as ws

    ws.add([{"code": "600519", "name": "贵州茅台"}])
    doc = client.get("/api/config/export?secrets=true&watchlist=true").json()
    assert doc["includes_secrets"] and doc["settings"]["ai_api_key"] == KEY_A and doc["settings"]["wecom_secret"] == "wecom-secret-fake"
    assert doc["settings"]["ai_profiles"][0]["api_key"] == KEY_A and doc["watchlist"][0]["code"] == "600519"


# ---------- 导入 ----------
def test_import_preview_apply_backup_and_keep_secrets(client):
    _seed(client)
    doc = client.get("/api/config/export?watchlist=true").json()
    doc["settings"]["wecom_corpid"] = "wwnew"
    doc["settings"]["watchlist_alerts"] = True
    doc["settings"]["retention_trading_days"] = "bad-type"  # 类型不对的跳过
    doc["settings"]["unknown_key"] = 1
    doc["watchlist"] = [{"code": "000001", "name": "平安银行"}]
    pv = client.post("/api/config/import/preview", json={"file": doc}).json()
    keys = {c["key"] for c in pv["changes"]}
    assert {"wecom_corpid", "watchlist_alerts"} <= keys and "unknown_key" not in keys and "retention_trading_days" not in keys
    assert "ai_api_key" not in keys and "wecom_secret" not in keys  # 文件没密钥 → 不动
    assert pv["watchlist_count"] == 1 and pv["includes_secrets"] is False
    r = client.post("/api/config/import", json={"file": doc}).json()
    assert r["ok"] and r["backup"].endswith("pre-config-import") and r["watchlist"] == 1
    from backend.app.core.config import read_settings_file
    from backend.app.services import backups

    raw = read_settings_file()
    assert raw["wecom_corpid"] == "wwnew" and raw["ai_api_key"] == KEY_A and raw["wecom_secret"] == "wecom-secret-fake"
    assert raw["ai_profiles"][0]["api_key"] == KEY_A
    b = backups.list_backups()[-1]
    assert json.loads((b / "settings.json").read_text(encoding="utf-8"))["wecom_corpid"] == "wwexport"
    from backend.app.services import watchlist_service as ws

    assert [x["code"] for x in ws.load()] == ["000001"]


def test_import_with_secrets_overwrites(client):
    _seed(client)
    doc = client.get("/api/config/export?secrets=true").json()
    doc["settings"]["ai_api_key"] = "sk-new-fake"
    doc["settings"]["ai_profiles"][0]["api_key"] = "sk-new-fake"
    pv = client.post("/api/config/import/preview", json={"file": doc}).json()
    sec = [c for c in pv["changes"] if c["key"] == "ai_api_key"][0]
    assert sec["old"] == "（密钥）" and sec["new"] == "（密钥）" and "sk-" not in json.dumps(pv)
    client.post("/api/config/import", json={"file": doc})
    from backend.app.core.config import read_settings_file

    assert read_settings_file()["ai_api_key"] == "sk-new-fake"


def test_import_rejects_bad_files(client):
    for bad in ("not json", {"format": "other"}, {"format": "limit-pattern-config", "version": 99, "settings": {}},
                {"format": "limit-pattern-config", "version": 1}):
        r = client.post("/api/config/import/preview", json={"file": bad})
        assert r.status_code == 422, bad
    r = client.post("/api/config/import/preview", json={"file": {"format": "limit-pattern-config", "version": 99, "settings": {}}})
    assert "升级" in r.json()["detail"]


def test_status_shows_current_model(client):
    _seed(client)
    from backend.app.services.wecom.commands import _model_line

    assert "model-a" in _model_line()
