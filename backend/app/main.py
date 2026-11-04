"""FastAPI 入口。默认 python -m backend.app.main 监听 127.0.0.1:8765。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.app import __version__
from backend.app.api.routes import router
from backend.app.core.config import static_dir
from backend.app.core.database import init_db
from backend.app.tasks.scheduler import start_scheduler, stop_scheduler


def _mount_static(app: FastAPI) -> None:
    directory = static_dir()
    if directory is None:
        return
    root = directory.resolve()
    index = root / "index.html"
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def index_page():
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="页面还没有构建")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="没有这个接口")
        candidate = (root / full_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=404, detail="没有这个页面")
        if candidate.is_file():
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="没有这个页面")


def bootstrap_if_empty() -> None:
    """库里还没有行情时，后台拉一次，不挡住服务启动。"""
    import os

    if os.environ.get("LP_AUTO_SYNC", "1") == "0":
        return
    from backend.app.core.database import session_scope
    from backend.app.models.tables import DailyQuote
    from backend.app.services.sync_service import start_sync

    with session_scope() as session:
        exists = session.query(DailyQuote.id).first()
    if exists is None:
        start_sync()


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db()
        from backend.app.core.config import migrate_settings

        migrate_settings()
        from backend.app.services import backups

        started = backups.on_start()
        from backend.app.core import auth as _auth

        _auth.migrate()  # 0.5.0 只有密码 → admin 账号
        from backend.app.core import ai_profiles as _aip

        _aip.public()  # 0.5.2：旧的单个 AI 配置迁移为第一个模型配置  # 新版本第一次启动：先备份设置和数据库（保留 3 份），再迁移
        if started["changed"]:
            from backend.app.services.wecom.menu import autosync_in_background

            autosync_in_background()
        from backend.app.services.retention import purge_excluded_boards

        purge_excluded_boards()
        from backend.app.core.database import session_scope
        from backend.app.services.intraday_service import purge_provisional

        with session_scope() as session:
            purge_provisional(session)
        start_scheduler()
        bootstrap_if_empty()
        import os as _os

        if _os.environ.get("LP_WARM", "1") != "0":
            from backend.app.services.backtest_service import warm_cache

            warm_cache()  # 后台线程，不阻塞启动
        yield
        stop_scheduler()

    app = FastAPI(title="三日涨停形态统计", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    @app.middleware("http")
    async def access_password(request: Request, call_next):
        from backend.app.core import auth

        path = request.url.path
        if request.method != "OPTIONS" and not auth.exempt(path) and auth.required() and not auth.valid(request.cookies.get(auth.COOKIE)):
            return JSONResponse({"detail": "需要登录", "login_required": True}, status_code=401)
        return await call_next(request)

    app.include_router(router, prefix="/api")
    from backend.app.api.wecom_routes import router as wecom_router

    app.include_router(wecom_router, prefix="/api")
    from backend.app.api.signals_routes import router as signals_router

    app.include_router(signals_router, prefix="/api")
    from backend.app.api.cleanup_routes import router as cleanup_router

    app.include_router(cleanup_router, prefix="/api")
    _mount_static(app)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    import os

    uvicorn.run("backend.app.main:app", host=os.environ.get("LP_HOST", "127.0.0.1"), port=int(os.environ.get("LP_PORT", "8765")), factory=False)


if __name__ == "__main__":
    main()
