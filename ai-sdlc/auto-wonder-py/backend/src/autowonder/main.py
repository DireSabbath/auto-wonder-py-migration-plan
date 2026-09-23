"""应用工厂与 ``autowonder-serve`` 入口。"""

from pathlib import Path

from fastapi import FastAPI

from autowonder import __version__
from autowonder.agents.router import router as agent_router
from autowonder.api.errors import install_exception_handlers
from autowonder.api.meta import router as meta_router
from autowonder.api.middleware import AuthMiddleware
from autowonder.api.spa import mount_spa
from autowonder.audits.router import router as audit_router
from autowonder.auth.router import router as auth_router
from autowonder.backups.router import router as backup_router
from autowonder.categories.router import router as category_router
from autowonder.clarifications.router import router as clarification_router
from autowonder.config import get_settings
from autowonder.core.logging import configure_logging
from autowonder.dashboards.router import router as dashboard_router
from autowonder.environments.router import router as environment_router
from autowonder.memories.router import router as memory_router
from autowonder.notifications.router import router as notification_router
from autowonder.platform.router import router as branding_router
from autowonder.repos.router import router as repo_router
from autowonder.sdlcs.router import router as sdlc_router
from autowonder.settings.router import router as setting_router
from autowonder.squads.router import router as squad_router
from autowonder.statemachines.router import router as status_template_router
from autowonder.templates.router import router as template_router
from autowonder.users.router import router as user_router
from autowonder.workspaces.router import router as workspace_router


def create_app() -> FastAPI:
    """装配中间件、异常处理和已迁移的路由。"""
    import autowonder.model_imports  # noqa: F401

    configure_logging()
    app = FastAPI(title="auto-wonder", version=__version__)
    app.add_middleware(AuthMiddleware)
    install_exception_handlers(app)
    app.include_router(meta_router)
    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(branding_router)
    app.include_router(agent_router)
    app.include_router(workspace_router)
    app.include_router(squad_router)
    app.include_router(sdlc_router)
    app.include_router(status_template_router)
    app.include_router(setting_router)
    app.include_router(category_router)
    app.include_router(clarification_router)
    app.include_router(memory_router)
    app.include_router(notification_router)
    app.include_router(dashboard_router)
    app.include_router(audit_router)
    app.include_router(backup_router)
    app.include_router(template_router)
    app.include_router(environment_router)
    app.include_router(repo_router)
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
