"""注册、登录、刷新与改密。行为对齐 ``UserService``。"""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.platform.service import ensure_system_admin
from autowonder.security.jwt import TokenPayload, sign_access
from autowonder.security.password import encode, matches
from autowonder.users.models import User
from autowonder.users.schemas import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RegisterRequest,
    UserView,
)
from autowonder.workspaces.models import OrgMember


async def find_user_by_username(session: AsyncSession, username: str) -> User | None:
    """按登录名查找未删除用户。"""
    return await session.scalar(
        select(User).where(User.username == username, User.is_deleted == 0).limit(1)
    )


async def find_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    """按 id 查找未删除用户。"""
    return await session.scalar(
        select(User).where(User.id == user_id, User.is_deleted == 0).limit(1)
    )


def to_view(user: User, include_admin: bool) -> UserView:
    """组装用户视图。注册响应不带 isAdmin，登录响应带。"""
    is_admin = None
    if include_admin:
        is_admin = user.is_admin == 1
    return UserView(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        email=user.email,
        is_admin=is_admin,
    )


async def register_user(session: AsyncSession, request: RegisterRequest) -> UserView:
    """创建用户。平台还没有管理员时，这次注册会成为管理员。"""
    if request.username is None or request.username.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID, "用户名不能为空")
    if await find_user_by_username(session, request.username) is not None:
        raise BizError(ErrorCode.CONFLICT, "用户名已存在")
    user = User(
        username=request.username,
        email=request.email,
        nickname=request.nickname,
        password_hash=encode(request.password or ""),
        status=0,
        is_admin=0,
        is_deleted=0,
    )
    session.add(user)
    await session.flush()
    await ensure_system_admin(session)
    await session.commit()
    return to_view(user, include_admin=False)


async def login_user(
    session: AsyncSession,
    request: LoginRequest,
    store_refresh: Callable[[str, int, int], Awaitable[None]],
) -> LoginResponse:
    """校验口令并签发访问令牌与刷新令牌。"""
    user = None
    if request.username is not None:
        user = await find_user_by_username(session, request.username)
    password_ok = False
    if user is not None and user.password_hash is not None and request.password is not None:
        password_ok = matches(request.password, user.password_hash)
    if not password_ok or user is None:
        raise BizError(ErrorCode.UNAUTHORIZED, "用户名或密码错误")
    if user.status == 1 and user.password_hash == "DEACTIVATED":
        raise BizError(ErrorCode.DEACTIVATION_ACCOUNT_DISABLED)
    jti = str(uuid.uuid4())
    access_token = sign_access(TokenPayload(user.id, None, jti))
    refresh_token = str(uuid.uuid4())
    await store_refresh(refresh_token, user.id, get_settings().jwt_refresh_ttl_seconds)
    return LoginResponse(
        user_id=user.id,
        access_token=access_token,
        refresh_token=refresh_token,
        user=to_view(user, include_admin=True),
    )


async def logout_user(
    request: LogoutRequest,
    revoke_refresh: Callable[[str], Awaitable[None]],
) -> None:
    """吊销刷新令牌。没有令牌时直接返回。"""
    if request.refresh_token is not None:
        await revoke_refresh(request.refresh_token)


async def refresh_access_token(
    session: AsyncSession,
    refresh_token: str | None,
    workspace_id: int | None,
    user_id_by_refresh: Callable[[str], Awaitable[int | None]],
) -> str:
    """用刷新令牌签发新的访问令牌。工作空间声明会再校验成员身份。"""
    if refresh_token is None or refresh_token.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID, "refreshToken 不能为空")
    user_id = await user_id_by_refresh(refresh_token)
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED, "刷新令牌无效或已过期")
    return sign_access(
        TokenPayload(
            user_id,
            await resolve_workspace_claim(session, user_id, workspace_id),
            str(uuid.uuid4()),
        )
    )


async def resolve_workspace_claim(
    session: AsyncSession,
    user_id: int,
    workspace_id: int | None,
) -> int | None:
    """调用方声明工作空间，服务端按有效成员身份决定是否写入 claim。"""
    if workspace_id is None:
        return None
    member = await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
    if member is None or member.status != 0 or member.is_deleted != 0:
        return None
    return workspace_id


async def change_password(
    session: AsyncSession,
    user_id: int,
    old_password: str | None,
    new_password: str | None,
) -> None:
    """校验旧口令后更新哈希。"""
    if old_password is None or old_password.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID, "旧密码不能为空")
    if new_password is None or new_password.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID, "新密码不能为空")
    user = await find_user_by_id(session, user_id)
    if user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    if user.password_hash is None or not matches(old_password, user.password_hash):
        raise BizError(ErrorCode.UNAUTHORIZED, "旧密码不正确")
    await session.execute(
        update(User)
        .where(User.id == user_id, User.is_deleted == 0)
        .values(password_hash=encode(new_password))
    )
    await session.commit()
