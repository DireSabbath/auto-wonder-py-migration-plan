"""``/api/workspaces/current/backups``。管理要求工作空间管理员。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.backups.archive import page_bounds
from autowonder.backups.service import create_backup, download_backup, list_backups
from autowonder.config import get_settings
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.storage.objects import get_object_storage

router = APIRouter(
    prefix="/api/workspaces/current/backups",
    tags=["backups"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "管理项目配置备份"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


@router.post("")
async def create(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """创建一份项目配置备份。"""
    settings = get_settings()
    return ok(
        await create_backup(
            session,
            get_object_storage(),
            _workspace_id(),
            _user_id(),
            settings.oss_backup_bucket,
            settings.oss_artifact_bucket,
            settings.oss_bucket,
        )
    )


@router.get("")
async def list_page(
    session: AsyncSession = Depends(get_session),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """分页列出备份历史。"""
    bounded_page, bounded_size = page_bounds(page, size)
    return ok(await list_backups(session, _workspace_id(), bounded_page, bounded_size))


@router.get("/{id}/download")
async def download(id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """取得成功备份的下载地址。"""
    url = await download_backup(session, get_object_storage(), _workspace_id(), id)
    return ok({"url": url})
