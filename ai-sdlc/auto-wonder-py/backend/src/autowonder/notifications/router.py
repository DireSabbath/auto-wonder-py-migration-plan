"""``/api/notifications``。登录后按当前工作空间和用户读写自己的通知。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.notifications.schemas import UpdatePrefRequest
from autowonder.notifications.service import (
    delete_notification,
    list_notifications,
    list_prefs,
    mark_all_read,
    mark_read,
    unread_count,
    update_prefs,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("")
async def list_page(
    session: AsyncSession = Depends(get_session),
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """分页列出当前用户的通知。"""
    return ok(await list_notifications(session, _workspace_id(), _user_id(), status, page, size))


@router.get("/unread-count")
async def unread(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """未读数量。"""
    return ok(await unread_count(session, _workspace_id(), _user_id()))


@router.post("/read-all")
async def read_all(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """全部标为已读。"""
    await mark_all_read(session, _workspace_id(), _user_id())
    return ok(None)


@router.get("/prefs")
async def prefs(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """通知偏好。"""
    return ok(await list_prefs(session, _workspace_id(), _user_id()))


@router.put("/prefs")
async def put_prefs(
    body: UpdatePrefRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新通知偏好。"""
    await update_prefs(session, body, _workspace_id(), _user_id())
    return ok(None)


@router.post("/{id}/read")
async def read(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """把一条通知标为已读。"""
    await mark_read(session, id, _workspace_id(), _user_id())
    return ok(None)


@router.delete("/{id}")
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """删除一条属于当前用户的通知。"""
    await delete_notification(session, id, _workspace_id(), _user_id())
    return ok(None)
