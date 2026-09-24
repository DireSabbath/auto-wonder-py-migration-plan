"""``/api/ai-usage``。查看用量要求只读，配额要求管理员。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.aiusage.schemas import UpdateQuotaRequest
from autowonder.aiusage.service import get_quota, list_usage, update_quota
from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session

router = APIRouter(
    prefix="/api/ai-usage",
    tags=["ai-usage"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看AI用量"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@router.get("")
async def list_page(
    period: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """列出一个周期内的用量。"""
    return ok(await list_usage(session, _workspace_id(), period))


@router.get(
    "/quota",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "查看AI用量配额"))],
)
async def read_quota(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """读取配额。"""
    return ok(await get_quota(session, _workspace_id()))


@router.put(
    "/quota",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "更新AI用量配额"))],
)
async def write_quota(
    body: UpdateQuotaRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """更新配额。"""
    await update_quota(session, body, _workspace_id())
    return ok(None)
