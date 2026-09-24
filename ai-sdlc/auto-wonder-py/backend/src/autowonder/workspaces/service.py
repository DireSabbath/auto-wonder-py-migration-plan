"""工作空间生命周期、成员和切换令牌。行为对齐 ``WorkspaceService``。"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, literal_column, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from autowonder.agents.seeder import seed as seed_platform_agent
from autowonder.api.access import WorkspaceAccessLevel
from autowonder.audits.service import AuditRecord, record_required
from autowonder.core.clock import now_local
from autowonder.core.context import current
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.page import PageResult
from autowonder.db.rows import rowcount
from autowonder.dispatch.service import stop_deleted_workspace_dispatches
from autowonder.platform.service import is_system_admin
from autowonder.scheduledtasks.service import DELETION_REASON, pause_active_by_workspace
from autowonder.security.jwt import TokenPayload, sign_access
from autowonder.statemachines.seeder import seed as seed_status_templates
from autowonder.users.models import User
from autowonder.users.service import find_user_by_id
from autowonder.workspaces.identity_tags import from_stored, normalize
from autowonder.workspaces.models import Org, OrgMember
from autowonder.workspaces.schemas import (
    CreateWorkspaceRequest,
    CurrentMembershipView,
    MemberCandidateView,
    MemberView,
    RecycleBinItem,
    RestoreWorkspaceRequest,
    SwitchWorkspaceResponse,
    WorkspaceUpdateRequest,
    WorkspaceView,
)

NAME_MAX_LENGTH = 128
DESCRIPTION_MAX_LENGTH = 512
RECYCLE_BIN_MAX_PAGE_SIZE = 100
MEMBER_CANDIDATE_LIMIT = 20
AUDIT_ACTOR_HUMAN = "HUMAN"
AUDIT_MODULE_ORG = "ORG"


async def count_usable(session: AsyncSession, workspace_id: int) -> int:
    """未删除且未停用的工作空间数量。"""
    result = await session.execute(
        select(func.count())
        .select_from(Org)
        .where(Org.id == workspace_id, Org.is_deleted == 0, Org.status == 0)
    )
    return int(result.scalar_one())


async def find_member(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
) -> OrgMember | None:
    """查找未删除的成员记录，不在这里判断 status。"""
    return await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )


async def create_workspace(
    session: AsyncSession,
    request: CreateWorkspaceRequest | None,
    owner_user_id: int,
) -> WorkspaceView:
    """创建工作空间，并把创建者写成 ADMIN，同时播种状态模版和平台数字人。"""
    if request is None:
        raise BizError(ErrorCode.WORKSPACE_NAME_REQUIRED)
    trimmed_name = require_name(request.name)
    if await _find_by_active_name(session, trimmed_name) is not None:
        raise BizError(ErrorCode.WORKSPACE_NAME_DUPLICATE)
    workspace = Org(
        name=trimmed_name,
        active_name_key=trimmed_name,
        description=normalize_description(request.description),
        background=normalize_background(request.background),
        owner_id=owner_user_id,
        status=0,
        creator_id=owner_user_id,
        version=0,
        is_deleted=0,
    )
    session.add(workspace)
    try:
        await session.flush()
    except IntegrityError as error:
        await session.rollback()
        if _duplicate_key(error):
            raise BizError(ErrorCode.WORKSPACE_NAME_DUPLICATE) from error
        raise
    session.add(
        OrgMember(
            tenant_id=workspace.id,
            user_id=owner_user_id,
            status=0,
            access_level=WorkspaceAccessLevel.ADMIN.name,
            identity_tags=[],
            creator_id=owner_user_id,
            joined_at=now_local(),
            is_deleted=0,
        )
    )
    await session.flush()
    await seed_status_templates(session, workspace.id, owner_user_id)
    await seed_platform_agent(session, workspace.id, owner_user_id)
    await session.commit()
    result = _to_view(workspace)
    result.access_level = WorkspaceAccessLevel.ADMIN.name
    result.is_owner = True
    result.can_manage = True
    return result


async def list_by_user(session: AsyncSession, user_id: int) -> list[WorkspaceView]:
    """当前用户加入的在用工作空间，带编辑弹层需要的完整字段。"""
    levels = await _levels_by_workspace(session, user_id)
    rows = (
        await session.scalars(
            select(Org)
            .join(OrgMember, Org.id == OrgMember.tenant_id)
            .where(
                OrgMember.user_id == user_id,
                OrgMember.status == 0,
                OrgMember.is_deleted == 0,
                Org.is_deleted == 0,
            )
            .order_by(Org.gmt_create.desc())
        )
    ).all()
    result: list[WorkspaceView] = []
    for workspace in rows:
        value = _to_view(workspace)
        access_level = exact_access_level(levels.get(workspace.id))
        value.access_level = access_level.name
        owner = workspace.owner_id == user_id
        value.is_owner = owner
        value.can_manage = owner or access_level == WorkspaceAccessLevel.ADMIN
        result.append(value)
    return result


async def get_current(session: AsyncSession, workspace_id: int) -> WorkspaceView:
    """读取在用工作空间。不存在时返回资源不存在。"""
    workspace = await _find_active(session, workspace_id)
    if workspace is None:
        raise BizError(ErrorCode.NOT_FOUND, "工作空间不存在")
    return _to_view(workspace)


async def switch_workspace(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
) -> SwitchWorkspaceResponse:
    """确认工作空间可用后签发带 workspace claim 的访问令牌。"""
    if await count_usable(session, workspace_id) == 0:
        raise BizError(ErrorCode.ORG_DELETED_OR_DISABLED)
    member = await find_member(session, workspace_id, user_id)
    if _active_member(member):
        access_level = exact_access_level(member.access_level if member is not None else None)
    elif await is_system_admin(session, user_id):
        access_level = WorkspaceAccessLevel.ADMIN
    else:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    ctx = current()
    ctx.workspace_id = workspace_id
    ctx.access_level = access_level.name
    access_token = sign_access(
        TokenPayload(user_id=user_id, workspace_id=workspace_id, jti=str(uuid.uuid4()))
    )
    return SwitchWorkspaceResponse(access_token=access_token, access_level=access_level.name)


async def list_members(session: AsyncSession, workspace_id: int) -> list[MemberView]:
    """列出状态正常的成员。"""
    workspace = await _find_active(session, workspace_id)
    members = (
        await session.scalars(
            select(OrgMember)
            .where(
                OrgMember.tenant_id == workspace_id,
                OrgMember.is_deleted == 0,
                OrgMember.status == 0,
            )
            .order_by(OrgMember.joined_at.asc())
        )
    ).all()
    result: list[MemberView] = []
    for member in members:
        value = MemberView(
            user_id=member.user_id,
            joined_at=member.joined_at,
            owner=workspace is not None and workspace.owner_id == member.user_id,
            access_level=exact_access_level(member.access_level).name,
            identity_tags=from_stored(member.identity_tags),
        )
        _apply_user(value, await find_user_by_id(session, member.user_id))
        result.append(value)
    return result


async def current_membership(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
) -> CurrentMembershipView:
    """当前请求用户的成员身份。平台管理员没有成员行时合成一条 ADMIN。"""
    member = await find_member(session, workspace_id, user_id)
    if not _active_member(member) and await is_system_admin(session, user_id):
        workspace = await _find_active(session, workspace_id)
        result = CurrentMembershipView(
            user_id=user_id,
            joined_at=None,
            owner=workspace is not None and workspace.owner_id == user_id,
            access_level=WorkspaceAccessLevel.ADMIN.name,
            identity_tags=from_stored(None),
        )
        _apply_current(result, await find_user_by_id(session, user_id))
        return result
    active = _require_active(member)
    workspace = await _find_active(session, workspace_id)
    result = CurrentMembershipView(
        user_id=active.user_id,
        joined_at=active.joined_at,
        owner=workspace is not None and workspace.owner_id == active.user_id,
        access_level=exact_access_level(active.access_level).name,
        identity_tags=from_stored(active.identity_tags),
    )
    _apply_current(result, await find_user_by_id(session, active.user_id))
    return result


async def search_member_candidates(
    session: AsyncSession,
    workspace_id: int,
    keyword: str | None,
) -> list[MemberCandidateView]:
    """搜索还不是活跃成员的正常用户，最多 20 条。"""
    normalized = "" if keyword is None else keyword.strip()
    member_exists = (
        select(OrgMember.id)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == User.id,
            OrgMember.is_deleted == 0,
            OrgMember.status == 0,
        )
        .exists()
    )
    statement = (
        select(User)
        .where(User.is_deleted == 0, User.status == 0, ~member_exists)
        .order_by(User.gmt_create.desc(), User.id.desc())
        .limit(MEMBER_CANDIDATE_LIMIT)
    )
    if normalized != "":
        like = f"%{normalized}%"
        statement = statement.where(
            or_(User.username.like(like), User.email.like(like), User.nickname.like(like))
        )
    users = (await session.scalars(statement)).all()
    return [
        MemberCandidateView(
            user_id=user.id,
            username=user.username,
            email=user.email,
            nickname=user.nickname,
        )
        for user in users
    ]


async def add_member(
    session: AsyncSession,
    workspace_id: int,
    target_user_id: int | None,
    operator_id: int,
) -> None:
    """把用户加为只读成员。已经是活跃成员时不改原级别。"""
    if target_user_id is None:
        raise BizError(ErrorCode.PARAM_INVALID, "用户不能为空")
    target_user = await find_user_by_id(session, target_user_id)
    if target_user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    existing = await find_member(session, workspace_id, target_user_id)
    if _active_member(existing):
        return
    if target_user.status != 0:
        raise BizError(ErrorCode.PARAM_INVALID, "用户不可添加")
    await _insert_or_activate(
        session,
        workspace_id,
        target_user_id,
        WorkspaceAccessLevel.READ_ONLY.name,
        [],
        operator_id,
        operator_id,
    )
    await session.commit()


async def update_member_access(
    session: AsyncSession,
    workspace_id: int,
    target_user_id: int,
    requested_level: str | None,
    operator_id: int,
) -> None:
    """修改成员访问级别，并写审计。"""
    level = _parse_level(requested_level, ErrorCode.WORKSPACE_ACCESS_LEVEL_INVALID)
    if target_user_id == operator_id:
        raise BizError(ErrorCode.WORKSPACE_SELF_LEVEL_MUTATION_FORBIDDEN)
    workspace = await _lock_active(session, workspace_id)
    if workspace is not None and workspace.owner_id == target_user_id:
        raise BizError(ErrorCode.WORKSPACE_OWNER_MUTATION_PROTECTED)
    target = _require_active(await _lock_member(session, workspace_id, target_user_id))
    old_level = exact_access_level(target.access_level)
    updated = await _update_member_column(
        session,
        workspace_id,
        target_user_id,
        {"access_level": level.name, "modifier_id": operator_id, "gmt_modified": now_local()},
    )
    _require_single_update(updated)
    audit = _member_audit(workspace_id, operator_id, target_user_id, "MEMBER_ACCESS_CHANGED")
    audit.add("oldAccessLevel", old_level.name)
    audit.add("newAccessLevel", level.name)
    audit.add("operatorId", operator_id)
    audit.add("targetUserId", target_user_id)
    await record_required(session, audit)
    await session.commit()


async def update_member_identity_tags(
    session: AsyncSession,
    workspace_id: int,
    target_user_id: int,
    requested_tags: list[str] | None,
    operator_id: int,
) -> None:
    """替换成员身份标签，并写审计。"""
    target = _require_active(await _lock_member(session, workspace_id, target_user_id))
    old_tags = from_stored(target.identity_tags)
    new_tags = normalize(requested_tags)
    updated = await _update_member_column(
        session,
        workspace_id,
        target_user_id,
        {
            "identity_tags": new_tags,
            "modifier_id": operator_id,
            "gmt_modified": now_local(),
        },
    )
    _require_single_update(updated)
    audit = _member_audit(
        workspace_id, operator_id, target_user_id, "MEMBER_IDENTITY_TAGS_CHANGED"
    )
    audit.add("oldIdentityTags", old_tags)
    audit.add("newIdentityTags", new_tags)
    audit.add("operatorId", operator_id)
    audit.add("targetUserId", target_user_id)
    await record_required(session, audit)
    await session.commit()


async def remove_member(
    session: AsyncSession,
    workspace_id: int,
    target_user_id: int,
    operator_id: int,
) -> None:
    """软删除成员。所有者不能被直接移除。"""
    workspace = await _lock_active(session, workspace_id)
    if workspace is not None and workspace.owner_id == target_user_id:
        raise BizError(ErrorCode.WORKSPACE_OWNER_MUTATION_PROTECTED)
    _require_active(await _lock_member(session, workspace_id, target_user_id))
    result = await session.execute(
        update(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == target_user_id,
            OrgMember.status == 0,
            OrgMember.is_deleted == 0,
        )
        .values(is_deleted=1, modifier_id=operator_id, gmt_modified=now_local())
    )
    _require_single_update(rowcount(result))
    await session.commit()


async def transfer_owner(
    session: AsyncSession,
    workspace_id: int,
    target_user_id: int,
    operator_id: int,
) -> None:
    """把所有者转给另一名成员。目标不是 ADMIN 时先提升。"""
    workspace = await _lock_active(session, workspace_id)
    if workspace is None or workspace.owner_id != operator_id or target_user_id == operator_id:
        raise _owner_transfer_invalid()
    current_owner = await _lock_member(session, workspace_id, operator_id)
    target = await _lock_member(session, workspace_id, target_user_id)
    if (
        not _active_member(current_owner)
        or current_owner is None
        or not is_exact_level(current_owner, WorkspaceAccessLevel.ADMIN)
        or not _active_member(target)
        or target is None
    ):
        raise _owner_transfer_invalid()
    try:
        target_level = exact_access_level(target.access_level)
    except BizError as error:
        raise _owner_transfer_invalid() from error
    if target_level != WorkspaceAccessLevel.ADMIN:
        promoted = await _update_member_column(
            session,
            workspace_id,
            target_user_id,
            {
                "access_level": WorkspaceAccessLevel.ADMIN.name,
                "modifier_id": operator_id,
                "gmt_modified": now_local(),
            },
        )
        if promoted != 1:
            raise _owner_transfer_invalid()
    owner_updated = rowcount(
        await session.execute(
            update(Org)
            .where(Org.id == workspace_id, Org.owner_id == operator_id, Org.is_deleted == 0)
            .values(owner_id=target_user_id, modifier_id=operator_id, gmt_modified=now_local())
        )
    )
    if owner_updated != 1:
        raise _owner_transfer_invalid()
    audit = _audit(workspace_id, operator_id, "ORG_OWNER_TRANSFERRED", "ORG", workspace_id)
    audit.add("oldOwnerId", operator_id)
    audit.add("newOwnerId", target_user_id)
    audit.add("operatorId", operator_id)
    audit.add("targetUserId", target_user_id)
    await record_required(session, audit)
    await session.commit()


async def update_workspace(
    session: AsyncSession,
    workspace_id: int,
    request: WorkspaceUpdateRequest | None,
    operator_id: int,
) -> WorkspaceView:
    """按 version 更新名称、描述和背景。"""
    if request is None:
        raise BizError(ErrorCode.PARAM_INVALID, "请求体不能为空")
    if request.version is None:
        raise BizError(ErrorCode.PARAM_INVALID, "version 不能为空")
    workspace = await _require_manageable(session, workspace_id, operator_id)
    name = require_name(request.name)
    description = normalize_description(request.description)
    background = normalize_background(request.background)
    if await _count_active_by_name(session, name, workspace_id) > 0:
        raise BizError(ErrorCode.WORKSPACE_NAME_DUPLICATE)
    updated = rowcount(
        await session.execute(
            update(Org)
            .where(Org.id == workspace_id, Org.is_deleted == 0, Org.version == request.version)
            .values(
                name=name,
                active_name_key=name,
                description=description,
                background=background,
                modifier_id=operator_id,
                version=Org.version + 1,
                gmt_modified=now_local(),
            )
        )
    )
    if updated != 1:
        raise BizError(ErrorCode.ORG_VERSION_CONFLICT)
    audit = _audit(workspace_id, operator_id, "ORG_UPDATED", "ORG", workspace_id)
    audit.add("oldName", workspace.name)
    audit.add("newName", name)
    audit.add("operatorId", operator_id)
    await record_required(session, audit)
    await session.commit()
    result = _to_view(workspace)
    result.name = name
    result.description = description
    result.background = background
    result.version = request.version + 1
    await _apply_manage_flags(session, result, workspace, operator_id)
    return result


async def delete_workspace(
    session: AsyncSession,
    workspace_id: int,
    operator_id: int,
) -> WorkspaceView:
    """逻辑删除并释放名称。定时任务在同一事务里暂停，在途派发在提交后处理。

    返回的是删除前读到的工作空间。SQL 会把 version 加一，但响应仍用删除前的值。
    """
    workspace = await _require_manageable(session, workspace_id, operator_id)
    result = _to_view(workspace)
    await _apply_manage_flags(session, result, workspace, operator_id)
    deleted = rowcount(
        await session.execute(
            update(Org)
            .where(Org.id == workspace_id, Org.is_deleted == 0)
            .values(
                is_deleted=1,
                active_name_key=None,
                deleted_at=now_local(),
                deleted_by=operator_id,
                modifier_id=operator_id,
                version=Org.version + 1,
                gmt_modified=now_local(),
            )
        )
    )
    if deleted != 1:
        raise BizError(ErrorCode.CONFLICT, "工作空间已被删除")
    paused_tasks = await pause_active_by_workspace(session, workspace_id, operator_id)
    audit = _audit(workspace_id, operator_id, "ORG_DELETED", "ORG", workspace_id)
    audit.add("name", workspace.name)
    audit.add("operatorId", operator_id)
    audit.add("reason", DELETION_REASON)
    audit.add("pausedScheduledTasks", paused_tasks)
    await record_required(session, audit)
    await session.commit()
    await stop_deleted_workspace_dispatches(session, workspace_id, operator_id)
    return result


async def page_recycle_bin(
    session: AsyncSession,
    keyword: str | None,
    page: int,
    size: int,
    operator_id: int,
) -> PageResult:
    """回收站。可见范围在 SQL 里决定：所有者、仍有效的 ADMIN，或平台管理员。"""
    normalized_page = max(page, 1)
    normalized_size = min(max(size, 1), RECYCLE_BIN_MAX_PAGE_SIZE)
    normalized_keyword = None
    if keyword is not None and keyword.strip() != "":
        normalized_keyword = keyword.strip()
    system_admin = await is_system_admin(session, operator_id)
    offset = (normalized_page - 1) * normalized_size
    condition = _recycle_filter(operator_id, system_admin, normalized_keyword)
    rows = (
        await session.scalars(
            select(Org)
            .where(*condition)
            .order_by(Org.deleted_at.desc(), Org.id.desc())
            .offset(offset)
            .limit(normalized_size)
        )
    ).all()
    total = int(
        (
            await session.execute(select(func.count()).select_from(Org).where(*condition))
        ).scalar_one()
    )
    taken = await _taken_active_names(session, [row.name for row in rows])
    names = await _user_names(session, rows)
    items: list[RecycleBinItem] = []
    for row in rows:
        items.append(
            RecycleBinItem(
                id=row.id,
                name=row.name,
                description=row.description,
                owner_id=row.owner_id,
                owner_name=names.get(row.owner_id),
                deleted_at=row.deleted_at,
                deleted_by=row.deleted_by,
                deleted_by_name=names.get(row.deleted_by) if row.deleted_by is not None else None,
                restorable=row.name not in taken,
            )
        )
    return PageResult.model_validate(
        {
            "list": items,
            "total": total,
            "pageNum": normalized_page,
            "pageSize": normalized_size,
        }
    )


async def restore_workspace(
    session: AsyncSession,
    workspace_id: int,
    request: RestoreWorkspaceRequest | None,
    operator_id: int,
) -> WorkspaceView:
    """恢复已删除工作空间。名称被占用时可以在同一次请求里改名。"""
    workspace = await _find_any(session, workspace_id)
    if workspace is None or not await _can_manage(session, workspace, operator_id):
        raise BizError(ErrorCode.ORG_NOT_FOUND_OR_NO_PERMISSION)
    if workspace.is_deleted != 1:
        result = _to_view(workspace)
        await _apply_manage_flags(session, result, workspace, operator_id)
        return result
    requested_name = None if request is None else request.new_name
    if requested_name is None or requested_name.strip() == "":
        name = require_name(workspace.name)
    else:
        name = require_name(requested_name)
    if await _count_active_by_name(session, name, None) > 0:
        raise BizError(ErrorCode.ORG_RESTORE_NAME_CONFLICT)
    shown_version = workspace.version
    restored = rowcount(
        await session.execute(
            update(Org)
            .where(Org.id == workspace_id, Org.is_deleted == 1)
            .values(
                is_deleted=0,
                name=name,
                active_name_key=name,
                deleted_at=None,
                deleted_by=None,
                modifier_id=operator_id,
                version=Org.version + 1,
                gmt_modified=now_local(),
            )
        )
    )
    if restored != 1:
        current_row = await _find_any(session, workspace_id)
        if current_row is None or current_row.is_deleted == 1:
            raise BizError(ErrorCode.ORG_NOT_FOUND_OR_NO_PERMISSION)
        result = _to_view(current_row)
        await _apply_manage_flags(session, result, current_row, operator_id)
        return result
    audit = _audit(workspace_id, operator_id, "ORG_RESTORED", "ORG", workspace_id)
    audit.add("name", name)
    audit.add("deletedName", workspace.name)
    audit.add("operatorId", operator_id)
    audit.add("scheduledTasksResumed", False)
    await record_required(session, audit)
    await session.commit()
    result = _to_view(workspace)
    result.name = name
    result.version = shown_version + 1
    await _apply_manage_flags(session, result, workspace, operator_id)
    return result


def require_name(raw_name: str | None) -> str:
    """去掉首尾空白，拒绝空名和超长名。"""
    if raw_name is None or raw_name.strip() == "":
        raise BizError(ErrorCode.WORKSPACE_NAME_REQUIRED)
    trimmed = raw_name.strip()
    if len(trimmed) > NAME_MAX_LENGTH:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"工作空间名称不能超过 {NAME_MAX_LENGTH} 个字符",
        )
    return trimmed


def normalize_description(description: str | None) -> str | None:
    """空白描述存成 null，超长则拒绝。"""
    if description is None or description.strip() == "":
        return None
    trimmed = description.strip()
    if len(trimmed) > DESCRIPTION_MAX_LENGTH:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"工作空间描述不能超过 {DESCRIPTION_MAX_LENGTH} 个字符",
        )
    return trimmed


def normalize_background(background: str | None) -> str | None:
    """空白背景存成 null。"""
    if background is None or background.strip() == "":
        return None
    return background.strip()


def exact_access_level(persisted_level: str | None) -> WorkspaceAccessLevel:
    """只接受枚举名。空值或未知值都是不合法级别。"""
    if persisted_level is None:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_LEVEL_INVALID)
    try:
        return WorkspaceAccessLevel[persisted_level]
    except KeyError as error:
        raise BizError(ErrorCode.WORKSPACE_ACCESS_LEVEL_INVALID) from error


def is_exact_level(member: OrgMember, expected: WorkspaceAccessLevel) -> bool:
    """级别无法解析时视为不匹配，供所有者转让的资格判断使用。"""
    try:
        return exact_access_level(member.access_level) == expected
    except BizError:
        return False


def _to_view(workspace: Org) -> WorkspaceView:
    return WorkspaceView(
        id=workspace.id,
        name=workspace.name,
        description=workspace.description,
        background=workspace.background,
        version=workspace.version,
    )


def _active_member(member: OrgMember | None) -> bool:
    return member is not None and member.status == 0 and member.is_deleted == 0


def _require_active(member: OrgMember | None) -> OrgMember:
    if not _active_member(member) or member is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return member


def _parse_level(requested_level: str | None, error_code: ErrorCode) -> WorkspaceAccessLevel:
    if requested_level is None or requested_level.strip() == "":
        raise BizError(error_code)
    try:
        return WorkspaceAccessLevel[requested_level]
    except KeyError as error:
        raise BizError(error_code) from error


def _require_single_update(updated: int) -> None:
    if updated != 1:
        raise BizError(ErrorCode.CONFLICT)


def _owner_transfer_invalid() -> BizError:
    return BizError(ErrorCode.WORKSPACE_OWNER_TRANSFER_INVALID)


def _duplicate_key(error: IntegrityError) -> bool:
    origin = error.orig
    if origin is None or not origin.args:
        return False
    return origin.args[0] == 1062


async def _find_active(session: AsyncSession, workspace_id: int) -> Org | None:
    return await session.scalar(
        select(Org).where(Org.id == workspace_id, Org.is_deleted == 0).limit(1)
    )


async def _find_any(session: AsyncSession, workspace_id: int) -> Org | None:
    return await session.scalar(select(Org).where(Org.id == workspace_id).limit(1))


async def _find_by_active_name(session: AsyncSession, name: str) -> Org | None:
    return await session.scalar(select(Org).where(Org.active_name_key == name).limit(1))


async def _lock_active(session: AsyncSession, workspace_id: int) -> Org | None:
    return await session.scalar(
        select(Org).where(Org.id == workspace_id, Org.is_deleted == 0).limit(1).with_for_update()
    )


async def _lock_member(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
) -> OrgMember | None:
    return await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
        .with_for_update()
    )


async def _count_active_by_name(
    session: AsyncSession,
    name: str,
    exclude_id: int | None,
) -> int:
    statement = select(func.count()).select_from(Org).where(Org.active_name_key == name)
    if exclude_id is not None:
        statement = statement.where(Org.id != exclude_id)
    return int((await session.execute(statement)).scalar_one())


async def _update_member_column(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
    values: dict[str, object],
) -> int:
    result = await session.execute(
        update(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user_id,
            OrgMember.status == 0,
            OrgMember.is_deleted == 0,
        )
        .values(**values)
    )
    return rowcount(result)


async def _levels_by_workspace(session: AsyncSession, user_id: int) -> dict[int, str]:
    rows = (
        await session.execute(
            select(Org.id, OrgMember.access_level)
            .join(OrgMember, Org.id == OrgMember.tenant_id)
            .where(
                OrgMember.user_id == user_id,
                OrgMember.status == 0,
                OrgMember.is_deleted == 0,
                Org.is_deleted == 0,
            )
            .order_by(Org.gmt_create.desc())
        )
    ).all()
    return {workspace_id: level for workspace_id, level in rows}


async def _require_manageable(
    session: AsyncSession,
    workspace_id: int,
    operator_id: int,
) -> Org:
    workspace = await _lock_active(session, workspace_id)
    if workspace is None or not await _can_manage(session, workspace, operator_id):
        raise BizError(ErrorCode.ORG_NOT_FOUND_OR_NO_PERMISSION)
    return workspace


async def _can_manage(session: AsyncSession, workspace: Org, operator_id: int) -> bool:
    if workspace.owner_id == operator_id:
        return True
    if await _admin_member(session, workspace.id, operator_id):
        return True
    return await is_system_admin(session, operator_id)


async def _admin_member(session: AsyncSession, workspace_id: int, operator_id: int) -> bool:
    member = await find_member(session, workspace_id, operator_id)
    return (
        _active_member(member)
        and member is not None
        and is_exact_level(member, WorkspaceAccessLevel.ADMIN)
    )


async def _apply_manage_flags(
    session: AsyncSession,
    value: WorkspaceView,
    workspace: Org,
    operator_id: int,
) -> None:
    owner = workspace.owner_id == operator_id
    value.is_owner = owner
    value.can_manage = (
        owner
        or await _admin_member(session, workspace.id, operator_id)
        or await is_system_admin(session, operator_id)
    )


def _recycle_filter(
    user_id: int,
    system_admin: bool,
    keyword: str | None,
) -> list[ColumnElement[bool]]:
    condition: list[ColumnElement[bool]] = [Org.is_deleted == 1]
    if not system_admin:
        admin_exists = (
            select(OrgMember.id)
            .where(
                OrgMember.tenant_id == Org.id,
                OrgMember.user_id == user_id,
                OrgMember.access_level == "ADMIN",
                OrgMember.status == 0,
                OrgMember.is_deleted == 0,
            )
            .exists()
        )
        condition.append(or_(Org.owner_id == user_id, admin_exists))
    if keyword is not None:
        condition.append(Org.name.like(f"%{keyword}%"))
    return condition


async def _taken_active_names(session: AsyncSession, names: list[str]) -> set[str]:
    if not names:
        return set()
    rows = (
        await session.scalars(select(Org.active_name_key).where(Org.active_name_key.in_(names)))
    ).all()
    return {name for name in rows if name is not None}


async def _user_names(session: AsyncSession, rows: Sequence[Org]) -> dict[int, str]:
    user_ids: set[int] = set()
    for row in rows:
        user_ids.add(row.owner_id)
        if row.deleted_by is not None:
            user_ids.add(row.deleted_by)
    if not user_ids:
        return {}
    users = (
        await session.scalars(select(User).where(User.is_deleted == 0, User.id.in_(user_ids)))
    ).all()
    return {user.id: _display_name(user) for user in users}


def _display_name(user: User) -> str:
    nickname = user.nickname
    if nickname is None or nickname.strip() == "":
        return user.username
    return nickname


async def _insert_or_activate(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
    access_level: str,
    identity_tags: list[str],
    creator_id: int,
    modifier_id: int,
) -> None:
    joined = now_local()
    stamp = joined.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
    statement = mysql_insert(OrgMember).values(
        tenant_id=workspace_id,
        user_id=user_id,
        status=0,
        joined_at=joined,
        access_level=access_level,
        identity_tags=identity_tags,
        creator_id=creator_id,
        modifier_id=modifier_id,
        is_deleted=0,
    )
    statement = statement.on_duplicate_key_update(
        joined_at=literal_column(f"IF(is_deleted = 1 OR status != 0, '{stamp}', joined_at)"),
        status=0,
        access_level=access_level,
        identity_tags=identity_tags,
        modifier_id=modifier_id,
        is_deleted=0,
        gmt_modified=joined,
    )
    await session.execute(statement)


def _apply_user(target: MemberView, user: User | None) -> None:
    if user is None:
        return
    target.username = user.username
    target.email = user.email
    target.nickname = user.nickname


def _apply_current(target: CurrentMembershipView, user: User | None) -> None:
    if user is None:
        return
    target.username = user.username
    target.email = user.email
    target.nickname = user.nickname


def _audit(
    workspace_id: int,
    operator_id: int,
    event: str,
    target_type: str,
    target_id: int,
) -> AuditRecord:
    return AuditRecord(
        tenant_id=workspace_id,
        actor_id=operator_id,
        actor_type=AUDIT_ACTOR_HUMAN,
        module=AUDIT_MODULE_ORG,
        action=event,
        target_type=target_type,
        target_id=target_id,
        event_type=event,
    )


def _member_audit(
    workspace_id: int,
    operator_id: int,
    target_user_id: int,
    event: str,
) -> AuditRecord:
    return _audit(workspace_id, operator_id, event, "MEMBER", target_user_id)
