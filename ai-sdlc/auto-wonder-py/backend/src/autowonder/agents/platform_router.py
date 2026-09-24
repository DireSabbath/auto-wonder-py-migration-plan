"""平台数字人状态与能力状态。两者都只要求工作空间只读。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.platform_intelligence import get_capability_status
from autowonder.agents.platform_status import get_platform_agent_status
from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session

agent_status_router = APIRouter(
    prefix="/api/platform-agent",
    tags=["platform-agent"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看平台智能体状态"))],
)

intelligence_router = APIRouter(
    prefix="/api/platform-intelligence",
    tags=["platform-intelligence"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看平台智能能力状态"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@agent_status_router.get("/status")
async def platform_agent_status(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """当前工作空间平台数字人的执行器配置和在线数。"""
    return ok(await get_platform_agent_status(session, _workspace_id()))


@intelligence_router.get("/status")
async def platform_intelligence_status(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """当前工作空间平台数字人是否具备可调用能力。"""
    return ok(await get_capability_status(session, _workspace_id()))
