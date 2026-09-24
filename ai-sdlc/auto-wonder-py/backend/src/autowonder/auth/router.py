"""``/api/auth`` 注册、登录、退出与刷新。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.auth.session import SessionService
from autowonder.core.redis import redis_client
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.users.schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
)
from autowonder.users.service import login_user, logout_user, refresh_access_token, register_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


def sessions() -> SessionService:
    """当前进程的会话存储。"""
    return SessionService(redis_client())


@router.post("/register")
async def register(
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建账号。"""
    return ok(await register_user(session, body))


@router.post("/login")
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """校验口令并返回访问令牌。"""
    store = sessions()
    return ok(await login_user(session, body, store.store_refresh))


@router.post("/logout")
async def logout(body: LogoutRequest) -> dict[str, Any]:
    """吊销刷新令牌。"""
    await logout_user(body, sessions().revoke_refresh)
    return ok(None)


@router.post("/refresh")
async def refresh(
    body: RefreshRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """用刷新令牌换新的访问令牌。"""
    store = sessions()
    access_token = await refresh_access_token(
        session,
        body.refresh_token,
        body.workspace_id,
        store.get_user_id_by_refresh,
    )
    return ok(RefreshResponse(access_token=access_token))
