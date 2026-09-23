"""应用工厂与 ``autowonder-serve`` 入口。"""

from pathlib import Path

from fastapi import FastAPI

from autowonder import __version__
from autowonder.api.errors import install_exception_handlers
from autowonder.api.meta import router as meta_router
from autowonder.api.middleware import AuthMiddleware
from autowonder.api.spa import mount_spa
from autowonder.auth.router import router as auth_router
from autowonder.config import get_settings
from autowonder.core.logging import configure_logging


def create_app() -> FastAPI:
    """装配中间件、异常处理和已迁移的路由。"""
    import autowonder.model_imports  # noqa: F401

    configure_logging()
    app = FastAPI(title="auto-wonder", version=__version__)
    app.add_middleware(AuthMiddleware)
    install_exception_handlers(app)
    app.include_router(meta_router)
    app.include_router(auth_router)
    dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if dist.is_dir():
        mount_spa(app, dist)
    return app


def serve() -> None:
    """生产入口：监听配置中的 HTTP 端口。"""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "autowonder.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=settings.http_port,
    )
