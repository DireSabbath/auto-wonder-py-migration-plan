"""``/api/audit-logs``。查看要求只读。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.audits.service import count_logs, search_logs
from autowonder.core.context import current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session

router = APIRouter(
    prefix="/api/audit-logs",
    tags=["audit-logs"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看审计日志"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("/count")
async def count(
    session: AsyncSession = Depends(get_session),
    module: str | None = None,
    action: str | None = None,
    actorType: str | None = None,
    actorId: int | None = None,
    targetType: str | None = None,
    targetId: int | None = None,
    startTime: str | None = None,
    endTime: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """符合条件的审计条数。"""
    return ok(
        await count_logs(
            session,
            _workspace_id(),
            module,
            action,
            actorType,
            actorId,
            targetType,
            targetId,
            startTime,
            endTime,
            keyword,
        )
    )


@router.get("")
async def search(
    session: AsyncSession = Depends(get_session),
    module: str | None = None,
    action: str | None = None,
    actorType: str | None = None,
    actorId: int | None = None,
    targetType: str | None = None,
    targetId: int | None = None,
    startTime: str | None = None,
    endTime: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """按条件分页查询审计日志。"""
    return ok(
        await search_logs(
            session,
            _workspace_id(),
            module,
            action,
            actorType,
            actorId,
            targetType,
            targetId,
            startTime,
            endTime,
            keyword,
            page,
            size,
        )
    )
