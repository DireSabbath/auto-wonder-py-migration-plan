"""``/api/dispatches``。查看要求只读。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.dispatch.query import get_dispatch, list_dispatches

router = APIRouter(
    prefix="/api/dispatches",
    tags=["dispatches"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看调度"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("")
async def list_page(
    page: Annotated[int, Query()] = 1,
    page_size: Annotated[int, Query(alias="page_size")] = 50,
    status: Annotated[str | None, Query()] = None,
    agent_id: Annotated[int | None, Query(alias="agent_id")] = None,
    workitem_id: Annotated[int | None, Query(alias="workitem_id")] = None,
    time_range: Annotated[str, Query(alias="time_range")] = "30d",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按状态、数字员工、工单和时间窗分页查询调度。"""
    return ok(
        await list_dispatches(
            session,
            _workspace_id(),
            status,
            agent_id,
            workitem_id,
            time_range,
            page,
            page_size,
        )
    )


@router.get("/{id}")
async def get_one(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """读取一条调度，并带上用户可见产物。"""
    return ok(await get_dispatch(session, _workspace_id(), id))
