from fastapi.testclient import TestClient


def test_frontend_dist_is_served_from_lp_static_dir(tmp_path, monkeypatch):
    page = tmp_path / "dist"
    page.mkdir()
    (page / "index.html").write_text("<html>首页</html>", encoding="utf-8")
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LP_STATIC_DIR", str(page))
    monkeypatch.setenv("LP_SCHEDULER", "0")
    monkeypatch.setenv("LP_AUTO_SYNC", "0")
    from backend.app.core.database import init_db, reset_engine
    from backend.app.main import create_app

    reset_engine()
    init_db()
    with TestClient(create_app()) as client:
        assert "首页" in client.get("/").text
        assert "首页" in client.get("/similarity").text
        assert client.get("/api/health").json()["status"] == "ok"
    reset_engine()


def test_views_kept_alive_and_capture_state_shared():
    """v0.4.4：切换左侧页面不丢数据——keep-alive + 手动采集共享状态 + 滚动位置按页面恢复。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "frontend" / "src"
    app = (src / "App.vue").read_text(encoding="utf-8")
    assert "<keep-alive" in app and "SettingsView" in app
    store = (src / "stores" / "capture.ts").read_text(encoding="utf-8")
    assert "reactive(" in store and "api.manualSnapshot" in store and "api.captureStatus" in store
    card = (src / "components" / "ManualCapture.vue").read_text(encoding="utf-8")
    assert "@/stores/capture" in card and "导出给AI分析" in card and "生成AI榜单" in card
    assert "scrollMemory" in (src / "router" / "index.ts").read_text(encoding="utf-8")
    assert "resume()" in (src / "views" / "AiRankingView.vue").read_text(encoding="utf-8")
