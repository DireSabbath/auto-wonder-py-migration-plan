"""``/api/debug-logs``。查看要求只读。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.debuglogs.service import list_debug_logs

router = APIRouter(
    prefix="/api/debug-logs",
    tags=["debug-logs"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看 Debug 日志"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("")
async def list_logs(
    sourceType: str,
    sourceId: int,
    agentId: Annotated[int | None, Query()] = None,
    since: Annotated[int | None, Query()] = None,
    page: Annotated[int, Query()] = 1,
    size: Annotated[int, Query()] = 50,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按执行主体查询 debug 日志。已上传行带十分钟下载地址。"""
    return ok(
        await list_debug_logs(
            session,
            _workspace_id(),
            sourceType,
            sourceId,
            agentId,
            since,
            page,
            size,
        )
    )
