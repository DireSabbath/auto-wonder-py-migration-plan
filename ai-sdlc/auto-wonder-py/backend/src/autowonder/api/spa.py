"""把 frontend/dist 作为 SPA 静态资源，未知路径回退到 index.html。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse


def mount_spa(app: FastAPI, dist: Path) -> None:
    """已构建的前端目录存在时提供静态文件与前端路由回退。"""
    index = dist / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        candidate = (dist / full_path).resolve()
        if candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)
