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


def bootstrap_data(moment: datetime | None = None) -> None:
    """启动时把库里缺的补齐：行情缺就补行情，派生层缺就补派生层。

    过去这一格只管「空库拉一次」，于是很常见的两种情形会一直卡在旧数据上：
    上一进程没赶上 17:10 那班车（电脑关着），行情就少一天；行情明明有这一天，
    可没人点过数据管理页的重建按钮，样本池里就是没有它。用户看到的都是同一句话
    ——「数据不会自动更新」。

    `LP_AUTO_SYNC=0` 是「开机什么都别自己动」那一档，两条路一起关掉。
    补行情那条把抓取上限钉在「已经收盘的那一天」：早上打开软件时当天那根线还没走完，
    把它当成一个完整交易日落进库里，三天形态窗口就多出半个日子。
    同步的收尾本来就会跑派生层，所以补了行情的那条路不再另起线程。
    `moment` 是判「收盘了没有」用的那把时钟，留空读真表；判据要用假时钟钉住日子。
    """
    import os

    if os.environ.get("LP_AUTO_SYNC", "1") == "0":
        return
    from backend.app.core.database import session_scope
    from backend.app.services import freshness
    from backend.app.services.sync_service import start_sync

    with session_scope() as session:
        verdict = freshness.gaps(session, moment)
    if verdict["quotes_behind"]:
        start_sync(end=verdict["expected_quote_day"])
        return
    freshness.start_derive(moment)


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
        bootstrap_data()
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
