"""FastAPI 入口。默认 python -m backend.app.main 监听 127.0.0.1:8765。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
        from backend.app.core.database import session_scope
        from backend.app.services import backtest_service
        from backend.app.services.retention import purge_excluded_boards

        purge_excluded_boards()
        #: 上一进程若死在一轮回测里，那一行 status 会停在 running；启动时没有后台线程能
        #: 替它收尾，就地记成 failed，免得回测页永远显示「正在回测」。
        with session_scope() as session:
            backtest_service.retire_stale_runs(session)
        start_scheduler()
        bootstrap_if_empty()
        yield
        stop_scheduler()

    app = FastAPI(title="三日涨停形态统计", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    _mount_static(app)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("backend.app.main:app", host="127.0.0.1", port=8765, factory=False)


if __name__ == "__main__":
    main()
