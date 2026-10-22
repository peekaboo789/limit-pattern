"""后端或前端尚未就绪时使用的最小 FastAPI 应用。

只保证 ``GET /api/health`` 符合契约，并托管一份静态页（优先 ``LP_STATIC_DIR``
指向的 ``frontend/dist``，否则是随包的 stub 页面）。其余 ``/api/*`` 返回
``{"detail": "stub 未实现该接口"}``，避免把页面误当成接口。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

try:
    from desktop import __version__ as APP_VERSION
except ImportError:  # 以脚本方式把 desktop/ 放在 sys.path 上时
    APP_VERSION = "0.1.0"


class SPAStaticFiles(StaticFiles):
    """目录请求返回 index.html；未知路径也回退到 index.html，供前端路由使用。"""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or scope.get("method") not in {"GET", "HEAD"}:
                raise
            normalized = path.replace("\\", "/")
            if normalized == "api" or normalized.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


def create_app(static_dir: str | os.PathLike[str] | None = None) -> FastAPI:
    directory = Path(static_dir) if static_dir is not None else Path(__file__).resolve().parent / "stub_static"
    app = FastAPI(title="三日涨停形态统计", version=f"{APP_VERSION}-stub", docs_url=None, redoc_url=None)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "version": f"{APP_VERSION}-stub", "data_date": None}

    @app.api_route(
        "/api/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def unimplemented(full_path: str) -> None:
        raise HTTPException(status_code=404, detail="stub 未实现该接口")

    if directory.is_dir() and (directory / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=str(directory), html=True), name="static")
    else:

        @app.get("/")
        def missing_static() -> None:
            raise HTTPException(status_code=503, detail="静态页面不存在")

    return app
