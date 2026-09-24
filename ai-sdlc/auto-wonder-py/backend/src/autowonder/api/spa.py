"""把 frontend/dist 作为 SPA 静态资源，未知路径回退到 index.html。"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from starlette.middleware.base import RequestResponseEndpoint


def mount_spa(app: FastAPI, dist: Path) -> None:
    """已构建的前端目录存在时提供静态文件。没有命中的 GET 回退到 index.html。

    ``/api`` 与 ``/ws`` 的 404 保持原响应。后注册的路由仍然先于这次回退。
    """
    root = dist.resolve()
    index = root / "index.html"

    @app.middleware("http")
    async def spa(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "GET" or request.method == "HEAD":
            static = _static_file(root, request.url.path)
            if static is not None:
                return static
        response = await call_next(request)
        if response.status_code != 404:
            return response
        path = request.url.path
        if path.startswith("/api") or path.startswith("/ws"):
            return response
        # 已经进了控制器的 404 是接口结果。Java 的 SPA 只接管没人处理的无点路径。
        if request.scope.get("endpoint") is not None:
            return response
        if request.method != "GET" and request.method != "HEAD":
            return response
        return FileResponse(index)


def _static_file(root: Path, path: str) -> FileResponse | None:
    relative = path.lstrip("/")
    if relative == "":
        return None
    candidate = (root / relative).resolve()
    if root not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return FileResponse(candidate)
