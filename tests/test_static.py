from fastapi.testclient import TestClient


def test_frontend_dist_is_served_from_lp_static_dir(tmp_path, monkeypatch):
    page = tmp_path / "dist"
    page.mkdir()
    (page / "index.html").write_text("<html>首页</html>", encoding="utf-8")
    monkeypatch.setenv("LP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LP_STATIC_DIR", str(page))
    monkeypatch.setenv("LP_SCHEDULER", "0")
    from backend.app.core.database import init_db, reset_engine
    from backend.app.main import create_app

    reset_engine()
    init_db()
    with TestClient(create_app()) as client:
        assert "首页" in client.get("/").text
        assert "首页" in client.get("/similarity").text
        assert client.get("/api/health").json()["status"] == "ok"
    reset_engine()
