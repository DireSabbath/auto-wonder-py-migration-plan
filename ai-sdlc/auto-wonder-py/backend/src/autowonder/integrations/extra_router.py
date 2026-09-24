"""Aone、回执、外部工单导入和单工单同步。能力查询仍留在原 router。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Match
from starlette.types import Scope

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.integrations.aone_codec import aone_enabled
from autowonder.integrations.aone_outbox import dispatch_pending
from autowonder.integrations.aone_schemas import AoneBindingRequest, AoneSyncNowRequest
from autowonder.integrations.aone_service import (
    create_binding,
    list_bindings,
    project_members,
    search_project_page,
    sync_local,
    sync_now,
    test_connection,
)
from autowonder.integrations.receipts import (
    ManualReceiptRequest,
    manual_confirm_succeeded,
    manual_retry,
)
from autowonder.integrations.workitem_import import (
    ExternalWorkitemImportRequest,
    import_workitem,
    list_records,
)


class AoneRoute(APIRoute):
    """Aone 关闭时控制器不存在，请求按未映射路径返回 404。"""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] == "http" and not aone_enabled():
            return Match.NONE, {}
        return super().matches(scope)


aone_router = APIRouter(
    prefix="/api/integrations/aone",
    tags=["aone"],
    route_class=AoneRoute,
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "管理Aone集成"))],
)
receipt_router = APIRouter(
    prefix="/api/integrations/receipts",
    tags=["integration-receipts"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "处理外部操作回执"))],
)
import_router = APIRouter(prefix="/api/v1/external/workitems", tags=["external-workitem-import"])
sync_router = APIRouter(
    prefix="/api/workitems",
    tags=["external-workitem-sync"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看外部工作项同步"))],
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


@aone_router.post("/bindings/test")
async def test_aone_connection(body: AoneBindingRequest) -> dict[str, Any]:
    """测试 Aone 连接。关闭开关时结果里带禁用原因。"""
    return ok(test_connection(body))


@aone_router.post("/bindings")
async def create_aone_binding(
    body: AoneBindingRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建或复用 Aone 项目绑定。"""
    view = await create_binding(session, body, _workspace_id(), _user_id())
    await session.commit()
    return ok(view)


@aone_router.get("/bindings")
async def list_aone_bindings(
    page: Annotated[int, Query()] = 1,
    size: Annotated[int, Query()] = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """分页列出 Aone 绑定。"""
    return ok(await list_bindings(session, _workspace_id(), page, size))


@aone_router.post("/projects/search")
async def search_aone_projects(
    body: AoneBindingRequest,
    q: Annotated[str, Query()] = "",
    page: Annotated[int, Query()] = 1,
    size: Annotated[int, Query()] = 20,
) -> dict[str, Any]:
    """用提交的凭据搜索 Aone 项目。"""
    return ok(search_project_page(body, q, page, size))


@aone_router.post("/projects/{projectId}/members")
async def list_aone_members(projectId: str, body: AoneBindingRequest) -> dict[str, Any]:
    """列出 Aone 项目成员。"""
    return ok(project_members(body, projectId))


@aone_router.post("/bindings/{id}/sync-now")
async def sync_aone_binding(
    id: int,
    body: AoneSyncNowRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """立即同步一个绑定下的工单。"""
    result = await sync_now(session, id, body.issue_ids, _workspace_id(), _user_id())
    await session.commit()
    return ok(result)


@aone_router.post("/outbox/dispatch-now")
async def dispatch_aone_outbox(
    limit: Annotated[int, Query()] = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """立刻派发待发送的外部写回。Aone 关闭时不取 AONE 行。"""
    count = await dispatch_pending(session, limit)
    await session.commit()
    return ok(count)


@receipt_router.post("/{id}/retry")
async def retry_receipt(
    id: int,
    body: ManualReceiptRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """人工重试一条回执。"""
    await manual_retry(
        session,
        id,
        _workspace_id(),
        _user_id(),
        None if body is None else body.reason,
    )
    await session.commit()
    return ok(None)


@receipt_router.post("/{id}/confirm-succeeded")
async def confirm_receipt(
    id: int,
    body: ManualReceiptRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """人工确认回执已经成功。"""
    await manual_confirm_succeeded(
        session,
        id,
        _workspace_id(),
        _user_id(),
        None if body is None else body.reason,
    )
    await session.commit()
    return ok(None)


@import_router.post(
    "/import",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "导入外部工单"))],
)
async def import_external_workitem(
    body: ExternalWorkitemImportRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """导入一条外部工单。"""
    result = await import_workitem(session, body, _workspace_id(), _user_id())
    await session.commit()
    return ok(result)


@import_router.get(
    "/import-records",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看外部工单导入记录"))],
)
async def list_import_records(
    sourceSystem: Annotated[str | None, Query()] = None,
    externalWorkitemId: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query()] = 1,
    size: Annotated[int, Query()] = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """查询外部工单导入记录。"""
    return ok(
        await list_records(
            session,
            sourceSystem,
            externalWorkitemId,
            status,
            _workspace_id(),
            page,
            size,
        )
    )


@sync_router.post(
    "/{id}/external-sync",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "同步外部工作项"))],
)
async def sync_external_workitem(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按本地工单刷新关联的 Aone 工单。"""
    result = await sync_local(session, id, _workspace_id(), _user_id())
    await session.commit()
    return ok(result)
