"""``/api/workspaces``。生命周期接口不套用令牌工作空间的访问级别。"""

from typing import Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session
from autowonder.workspaces.access_requests import (
    approve,
    cancel_request,
    list_all,
    list_for_workspace,
    reject,
    submit_request,
)
from autowonder.workspaces.schemas import (
    AddMemberRequest,
    CreateWorkspaceRequest,
    RejectAccessRequestBody,
    RestoreWorkspaceRequest,
    SubmitAccessRequestBody,
    TransferOwnerRequest,
    UpdateMemberAccessRequest,
    UpdateMemberIdentityTagsRequest,
    WorkspaceUpdateRequest,
)
from autowonder.workspaces.service import (
    add_member,
    create_workspace,
    current_membership,
    delete_workspace,
    get_current,
    list_by_user,
    list_members,
    page_recycle_bin,
    remove_member,
    restore_workspace,
    search_member_candidates,
    switch_workspace,
    transfer_owner,
    update_member_access,
    update_member_identity_tags,
    update_workspace,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


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


@router.post("")
async def create(
    body: CreateWorkspaceRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建工作空间。"""
    return ok(await create_workspace(session, body, _user_id()))


@router.get("/mine")
async def mine(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """我加入的工作空间。"""
    return ok(await list_by_user(session, _user_id()))


@router.post("/{workspace_id}/switch")
async def switch(
    workspace_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """切换当前工作空间并换发访问令牌。"""
    return ok(await switch_workspace(session, workspace_id, _user_id()))


@router.get("/all")
async def all_workspaces(
    session: AsyncSession = Depends(get_session),
    keyword: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """分页浏览在用工作空间。"""
    normalized_page = max(page, 1)
    normalized_size = min(max(size, 1), 100)
    return ok(await list_all(session, keyword, normalized_page, normalized_size, _user_id()))


@router.put("/{workspace_id}")
async def update(
    workspace_id: int,
    body: WorkspaceUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """修改工作空间资料。"""
    return ok(await update_workspace(session, workspace_id, body, _user_id()))


@router.delete("/{workspace_id}")
async def delete(
    workspace_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """逻辑删除工作空间。"""
    return ok(await delete_workspace(session, workspace_id, _user_id()))


@router.get("/recycle-bin")
async def recycle_bin(
    session: AsyncSession = Depends(get_session),
    keyword: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """回收站。"""
    return ok(await page_recycle_bin(session, keyword, page, size, _user_id()))


@router.post("/{workspace_id}/restore")
async def restore(
    workspace_id: int,
    session: AsyncSession = Depends(get_session),
    body: RestoreWorkspaceRequest | None = Body(default=None),
) -> dict[str, Any]:
    """恢复已删除的工作空间。"""
    return ok(await restore_workspace(session, workspace_id, body, _user_id()))


@router.post("/{workspace_id}/access-requests")
async def submit_access_request(
    workspace_id: int,
    session: AsyncSession = Depends(get_session),
    body: SubmitAccessRequestBody | None = Body(default=None),
) -> dict[str, Any]:
    """申请加入工作空间。"""
    requested = None if body is None else body.requested_level
    await submit_request(session, workspace_id, requested, _user_id())
    return ok(None)


@router.post("/{workspace_id}/access-requests/{request_id}/cancel")
async def cancel_access_request(
    workspace_id: int,
    request_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """申请人撤销待审申请。"""
    await cancel_request(session, workspace_id, request_id, _user_id())
    return ok(None)


@router.get(
    "/current",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看当前工作空间"))],
)
async def current_workspace(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """当前令牌指向的工作空间。"""
    return ok(await get_current(session, _workspace_id()))


@router.get(
    "/current/membership",
    dependencies=[
        Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看当前工作空间成员身份"))
    ],
)
async def membership(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """当前用户在当前工作空间的身份。"""
    return ok(await current_membership(session, _workspace_id(), _user_id()))


@router.get(
    "/current/members",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看工作空间成员"))],
)
async def members(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """成员列表。"""
    return ok(await list_members(session, _workspace_id()))


@router.get(
    "/current/member-candidates",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "搜索工作空间成员候选人"))],
)
async def member_candidates(
    session: AsyncSession = Depends(get_session),
    keyword: str | None = None,
) -> dict[str, Any]:
    """搜索可添加的用户。"""
    return ok(await search_member_candidates(session, _workspace_id(), keyword))


@router.post(
    "/current/members",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "添加工作空间成员"))],
)
async def add_current_member(
    body: AddMemberRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """添加成员。"""
    await add_member(session, _workspace_id(), body.user_id, _user_id())
    return ok(None)


@router.delete(
    "/current/members/{user_id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "移除工作空间成员"))],
)
async def remove_current_member(
    user_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """移除成员。"""
    await remove_member(session, _workspace_id(), user_id, _user_id())
    return ok(None)


@router.put(
    "/current/members/{user_id}/access-level",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "修改工作空间成员访问级别"))],
)
async def change_access_level(
    user_id: int,
    body: UpdateMemberAccessRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """修改成员访问级别。"""
    await update_member_access(session, _workspace_id(), user_id, body.access_level, _user_id())
    return ok(None)


@router.put(
    "/current/members/{user_id}/identity-tags",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "修改工作空间成员身份标签"))],
)
async def change_identity_tags(
    user_id: int,
    body: UpdateMemberIdentityTagsRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """修改成员身份标签。"""
    await update_member_identity_tags(
        session, _workspace_id(), user_id, body.identity_tags, _user_id()
    )
    return ok(None)


@router.post(
    "/current/owner/transfer",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "转让工作空间所有者"))],
)
async def transfer(
    body: TransferOwnerRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """转让所有者。"""
    if body.target_user_id is None:
        raise BizError(ErrorCode.WORKSPACE_OWNER_TRANSFER_INVALID)
    await transfer_owner(session, _workspace_id(), body.target_user_id, _user_id())
    return ok(None)


@router.get(
    "/current/access-requests",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "查看工作空间权限申请"))],
)
async def access_requests(
    session: AsyncSession = Depends(get_session),
    status: str = "PENDING",
) -> dict[str, Any]:
    """查看权限申请。"""
    return ok(await list_for_workspace(session, _workspace_id(), status))


@router.post(
    "/current/access-requests/{request_id}/approve",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "通过工作空间权限申请"))],
)
async def approve_access_request(
    request_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """通过权限申请。"""
    await approve(session, _workspace_id(), request_id, _user_id())
    return ok(None)


@router.post(
    "/current/access-requests/{request_id}/reject",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.ADMIN, "拒绝工作空间权限申请"))],
)
async def reject_access_request(
    request_id: int,
    session: AsyncSession = Depends(get_session),
    body: RejectAccessRequestBody | None = Body(default=None),
) -> dict[str, Any]:
    """拒绝权限申请。"""
    reason = None if body is None else body.reason
    await reject(session, _workspace_id(), request_id, _user_id(), reason)
    return ok(None)
