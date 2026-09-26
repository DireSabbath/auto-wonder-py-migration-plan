"""工作空间加入申请。状态机对齐 ``AccessRequestService``。"""

import logging

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.page import PageResult
from autowonder.db.rows import rowcount
from autowonder.platform.service import is_system_admin
from autowonder.users.models import User
from autowonder.workitems.view import person_name
from autowonder.workspaces.identity_tags import normalize
from autowonder.workspaces.models import Org, OrgMember, WorkspaceAccessRequest
from autowonder.workspaces.schemas import AccessRequestView, WorkspaceListItem
from autowonder.workspaces.service import (
    _duplicate_key,
    _insert_or_activate,
    _levels_by_workspace,
    find_member,
)

logger = logging.getLogger(__name__)

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
REVIEWABLE_STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED})
MEMBERSHIP_MEMBER = "MEMBER"
MEMBERSHIP_PENDING = "PENDING"
MEMBERSHIP_NOT_MEMBER = "NOT_MEMBER"


async def list_all(
    session: AsyncSession,
    keyword: str | None,
    page: int,
    size: int,
    current_user_id: int,
) -> PageResult:
    """分页列出在用工作空间，并标出当前用户的成员或待审状态。"""
    offset = (page - 1) * size
    condition = [Org.is_deleted == 0]
    if keyword is not None and keyword != "":
        like = f"%{keyword}%"
        condition.append(or_(Org.name.like(like), Org.description.like(like)))
    workspaces = (
        await session.scalars(
            select(Org)
            .where(*condition)
            .order_by(Org.gmt_create.desc(), Org.id.desc())
            .offset(offset)
            .limit(size)
        )
    ).all()
    total = int(
        (
            await session.execute(select(func.count()).select_from(Org).where(*condition))
        ).scalar_one()
    )
    system_admin = await is_system_admin(session, current_user_id)
    levels = await _levels_by_workspace(session, current_user_id)
    pending_rows = (
        await session.scalars(
            select(WorkspaceAccessRequest).where(
                WorkspaceAccessRequest.requester_id == current_user_id,
                WorkspaceAccessRequest.status == STATUS_PENDING,
            )
        )
    ).all()
    pending_ids = {row.tenant_id: row.id for row in pending_rows}
    items: list[WorkspaceListItem] = []
    for workspace in workspaces:
        item = WorkspaceListItem(
            id=workspace.id,
            name=workspace.name,
            description=workspace.description,
            version=workspace.version,
        )
        member_level = levels.get(workspace.id)
        if member_level is not None:
            item.membership_status = MEMBERSHIP_MEMBER
            item.access_level = member_level
        elif workspace.id in pending_ids:
            item.membership_status = MEMBERSHIP_PENDING
            item.pending_request_id = pending_ids[workspace.id]
        else:
            item.membership_status = MEMBERSHIP_NOT_MEMBER
        owner = workspace.owner_id == current_user_id
        item.is_owner = owner
        item.can_manage = owner or member_level == WorkspaceAccessLevel.ADMIN.name or system_admin
        items.append(item)
    return PageResult.model_validate(
        {"list": items, "total": total, "pageNum": page, "pageSize": size}
    )


async def submit_request(
    session: AsyncSession,
    workspace_id: int,
    requested_level: str | None,
    requester_id: int,
) -> None:
    """提交加入申请。已是活跃成员或已有待审申请时拒绝。"""
    level = _parse_requested_level(requested_level)
    workspace = await session.scalar(
        select(Org).where(Org.id == workspace_id, Org.is_deleted == 0).limit(1)
    )
    if workspace is None:
        raise BizError(ErrorCode.NOT_FOUND)
    member = await find_member(session, workspace_id, requester_id)
    if member is not None and member.status == 0:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_ALREADY_MEMBER)
    existing = await session.scalar(
        select(WorkspaceAccessRequest)
        .where(
            WorkspaceAccessRequest.tenant_id == workspace_id,
            WorkspaceAccessRequest.requester_id == requester_id,
            WorkspaceAccessRequest.status == STATUS_PENDING,
        )
        .limit(1)
    )
    if existing is not None:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_DUPLICATE)
    request = WorkspaceAccessRequest(
        tenant_id=workspace_id,
        requester_id=requester_id,
        requested_level=level.name,
        status=STATUS_PENDING,
    )
    session.add(request)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        if _duplicate_key(error):
            raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_DUPLICATE) from error
        raise
    await session.commit()
    await _notify_submitted(session, workspace, requester_id)


async def list_for_workspace(
    session: AsyncSession,
    workspace_id: int,
    status: str | None,
) -> list[AccessRequestView]:
    """按状态列出当前工作空间的申请。"""
    if status is None or status.strip() == "" or status not in REVIEWABLE_STATUSES:
        raise BizError(ErrorCode.PARAM_INVALID, "Invalid access request status")
    requests = (
        await session.scalars(
            select(WorkspaceAccessRequest)
            .where(
                WorkspaceAccessRequest.tenant_id == workspace_id,
                WorkspaceAccessRequest.status == status,
            )
            .order_by(WorkspaceAccessRequest.gmt_create.desc())
        )
    ).all()
    if not requests:
        return []
    user_ids = {request.requester_id for request in requests}
    user_ids.update(request.reviewer_id for request in requests if request.reviewer_id is not None)
    names: dict[int, str | None] = {}
    if user_ids:
        users = (
            await session.scalars(select(User).where(User.is_deleted == 0, User.id.in_(user_ids)))
        ).all()
        names = {user.id: user.nickname for user in users}
    return [
        AccessRequestView(
            id=request.id,
            tenant_id=request.tenant_id,
            requester_id=request.requester_id,
            requester_name=names.get(request.requester_id),
            requested_level=request.requested_level,
            status=request.status,
            reviewer_id=request.reviewer_id,
            reviewer_name=(
                names.get(request.reviewer_id) if request.reviewer_id is not None else None
            ),
            reject_reason=request.reject_reason,
            gmt_create=request.gmt_create,
        )
        for request in requests
    ]


async def approve(
    session: AsyncSession,
    workspace_id: int,
    request_id: int,
    reviewer_id: int,
) -> None:
    """通过申请。申请人已经是活跃成员时不覆盖其级别和标签。"""
    request = await _require_pending(session, workspace_id, request_id)
    updated = await _update_status(session, request_id, STATUS_APPROVED, reviewer_id, None)
    if updated == 0:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_NOT_FOUND)
    existing = await find_member(session, workspace_id, request.requester_id)
    if existing is None or existing.status != 0:
        await _insert_or_activate(
            session,
            workspace_id,
            request.requester_id,
            request.requested_level,
            normalize([]),
            reviewer_id,
            reviewer_id,
        )
    await session.commit()
    await _notify_reviewed(session, workspace_id, request.requester_id, reviewer_id, True, None)


async def reject(
    session: AsyncSession,
    workspace_id: int,
    request_id: int,
    reviewer_id: int,
    reason: str | None,
) -> None:
    """拒绝申请，并保存原因。"""
    request = await _require_pending(session, workspace_id, request_id)
    updated = await _update_status(session, request_id, STATUS_REJECTED, reviewer_id, reason)
    if updated == 0:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_NOT_FOUND)
    await session.commit()
    await _notify_reviewed(
        session,
        workspace_id,
        request.requester_id,
        reviewer_id,
        False,
        reason,
    )


async def cancel_request(
    session: AsyncSession,
    workspace_id: int,
    request_id: int,
    operator_id: int,
) -> None:
    """申请人撤销自己仍在待审的申请。记录物理删除。"""
    request = await session.get(WorkspaceAccessRequest, request_id)
    if request is None or request.tenant_id != workspace_id:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_NOT_FOUND)
    if request.status != STATUS_PENDING:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_NOT_PENDING)
    if request.requester_id != operator_id:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_NOT_REQUESTER)
    deleted = rowcount(
        await session.execute(
            delete(WorkspaceAccessRequest).where(
                WorkspaceAccessRequest.id == request_id,
                WorkspaceAccessRequest.status == STATUS_PENDING,
            )
        )
    )
    if deleted == 0:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_NOT_FOUND)
    await session.commit()
    logger.info(
        "workspace access request cancelled tenantId=%s requestId=%s operatorId=%s",
        workspace_id,
        request_id,
        operator_id,
    )


def access_request_content(requester_name: str, workspace_name: str) -> str:
    """加入申请发给审核人的摘要。"""
    return requester_name + " 申请加入「" + workspace_name + "」"


def access_review_content(
    reviewer_name: str,
    workspace_name: str,
    approved: bool,
    reason: str | None,
) -> str:
    """审核结果发给申请人的摘要。"""
    action = "已通过"
    if not approved:
        action = "已拒绝"
    text = reviewer_name + action + "你加入「" + workspace_name + "」的申请"
    if reason is not None and reason.strip() != "":
        clipped = reason.strip()
        if len(clipped) > 180:
            clipped = clipped[:180]
        text = text + "：" + clipped
    if len(text) > 1024:
        return text[:1024]
    return text


async def _notify_submitted(
    session: AsyncSession,
    workspace: Org,
    requester_id: int,
) -> None:
    from autowonder.notifications.service import publish

    try:
        await publish(
            session,
            workspace.id,
            "WORKSPACE_ACCESS_REQUEST",
            "有人申请加入工作空间",
            access_request_content(await _display_name(session, requester_id), workspace.name),
            "/settings/members",
            "WORKSPACE",
            workspace.id,
            await _reviewer_ids(session, workspace),
        )
    except Exception:
        logger.exception(
            "failed to notify workspace access request tenantId=%s requesterId=%s",
            workspace.id,
            requester_id,
        )
        await session.rollback()


async def _notify_reviewed(
    session: AsyncSession,
    workspace_id: int,
    requester_id: int,
    reviewer_id: int,
    approved: bool,
    reason: str | None,
) -> None:
    from autowonder.notifications.service import publish

    title = "加入申请已通过"
    if not approved:
        title = "加入申请已拒绝"
    try:
        workspace = await session.get(Org, workspace_id)
        if workspace is None:
            raise RuntimeError("workspace disappeared before access review notice")
        await publish(
            session,
            workspace_id,
            "WORKSPACE_ACCESS_REVIEWED",
            title,
            access_review_content(
                await _display_name(session, reviewer_id),
                workspace.name,
                approved,
                reason,
            ),
            "/workspaces",
            "WORKSPACE",
            workspace_id,
            [requester_id],
        )
    except Exception:
        logger.exception(
            "failed to notify workspace access review tenantId=%s requesterId=%s",
            workspace_id,
            requester_id,
        )
        await session.rollback()


async def _reviewer_ids(session: AsyncSession, workspace: Org) -> list[int]:
    ids = [workspace.owner_id]
    admins = await session.scalars(
        select(OrgMember.user_id).where(
            OrgMember.tenant_id == workspace.id,
            OrgMember.status == 0,
            OrgMember.is_deleted == 0,
            OrgMember.access_level == "ADMIN",
        )
    )
    for user_id in admins:
        if user_id not in ids:
            ids.append(user_id)
    return ids


async def _display_name(session: AsyncSession, user_id: int) -> str:
    user = await session.get(User, user_id)
    if user is None:
        return str(user_id)
    name = person_name(user.nickname, user.username)
    if name is None or name.strip() == "":
        return str(user_id)
    return name


def _parse_requested_level(requested_level: str | None) -> WorkspaceAccessLevel:
    if requested_level is None or requested_level.strip() == "":
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_LEVEL_INVALID)
    try:
        return WorkspaceAccessLevel[requested_level]
    except KeyError as error:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_LEVEL_INVALID) from error


async def _require_pending(
    session: AsyncSession,
    workspace_id: int,
    request_id: int,
) -> WorkspaceAccessRequest:
    request = await session.get(WorkspaceAccessRequest, request_id)
    if request is None or request.tenant_id != workspace_id or request.status != STATUS_PENDING:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_REQUEST_NOT_FOUND)
    return request


async def _update_status(
    session: AsyncSession,
    request_id: int,
    status: str,
    reviewer_id: int,
    reason: str | None,
) -> int:
    result = await session.execute(
        update(WorkspaceAccessRequest)
        .where(
            WorkspaceAccessRequest.id == request_id,
            WorkspaceAccessRequest.status == STATUS_PENDING,
        )
        .values(
            status=status,
            reviewer_id=reviewer_id,
            reject_reason=reason,
            gmt_modified=now_local(),
        )
    )
    return rowcount(result)
