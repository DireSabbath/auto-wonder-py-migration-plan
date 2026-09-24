"""``/api/platform/admins``。不走工作空间权限，只认平台管理员标记。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.context import current_user_id
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.platform.admins import (
    add_platform_admin,
    list_platform_admins,
    remove_platform_admin,
    search_platform_admin_candidates,
)
from autowonder.platform.schemas import AddPlatformAdminRequest
from autowonder.platform.service import require_system_admin

router = APIRouter(prefix="/api/platform/admins", tags=["platform-admins"])


@router.get("")
async def list_admins(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """已登录用户读取名册。``canManage`` 表示调用者能否增减管理员。"""
    return ok(await list_platform_admins(session, current_user_id()))


@router.get("/candidates")
async def search_candidates(
    keyword: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """平台管理员按关键字搜索可提升的用户。"""
    await require_system_admin(session, current_user_id(), "搜索平台管理员候选人")
    return ok(await search_platform_admin_candidates(session, keyword))


@router.post("")
async def add_admin(
    body: AddPlatformAdminRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """提升后返回刷新过的名册。"""
    operator_id = current_user_id()
    await add_platform_admin(session, operator_id, body.user_id)
    await session.commit()
    return ok(await list_platform_admins(session, operator_id))


@router.delete("/{userId}")
async def remove_admin(
    userId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """撤销后返回刷新过的名册。"""
    operator_id = current_user_id()
    await remove_platform_admin(session, operator_id, userId)
    await session.commit()
    return ok(await list_platform_admins(session, operator_id))
