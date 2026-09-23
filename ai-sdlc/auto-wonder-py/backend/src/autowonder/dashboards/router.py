"""``/api/dashboard``。查看要求只读。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.dashboards.service import (
    get_agent_running,
    get_realtime,
    get_running_workitems,
    get_today_completed,
    get_week_completed,
)
from autowonder.db.session import get_session

router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看仪表盘"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("/realtime")
async def realtime(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """实时仪表盘。"""
    return ok(await get_realtime(session, _workspace_id()))


@router.get("/agents/{id}/running")
async def agent_running(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """某数字员工当前运行中的工单调度。"""
    return ok(await get_agent_running(session, _workspace_id(), id))


@router.get("/completed/today")
async def completed_today(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """今日端到端成功工单。"""
    return ok(await get_today_completed(session, _workspace_id()))


@router.get("/completed/week")
async def completed_week(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """本周端到端成功工单。"""
    return ok(await get_week_completed(session, _workspace_id()))


@router.get("/running")
async def running(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """全部运行中的工单调度。"""
    return ok(await get_running_workitems(session, _workspace_id()))
