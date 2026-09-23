"""数字员工生命周期。状态机、草稿克隆和乐观锁对齐 AgentService。"""

from typing import cast

from sqlalchemy import and_, delete, func, literal_column, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.branches import decode, encode
from autowonder.agents.evolution import (
    build_identity_map,
    evolution_mode_of_identity,
    identity_column_text,
    identity_with_evolution_mode,
)
from autowonder.agents.models import (
    Agent,
    AgentEnvironmentVariableRef,
    AgentMemoryRef,
    AgentRepoPerm,
    AgentSkill,
    AgentVersion,
)
from autowonder.agents.schemas import (
    AgentVersionSummaryView,
    AgentVersionView,
    AgentView,
    CreateAgentRequest,
    EnvironmentVariableRefView,
    MemoryRefItem,
    MemoryRefRequest,
    RepoPermItem,
    RepoPermRequest,
    SkillItem,
    SkillRequest,
    UpdateAgentRequest,
    UpdateConfigRequest,
)
from autowonder.api.access import WorkspaceAccessLevel
from autowonder.core.context import current
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.environments.models import EnvironmentVariable
from autowonder.executors.models import Executor
from autowonder.executors.registry import is_online
from autowonder.memories.models import Memory
from autowonder.skills.models import Skill
from autowonder.squads.attribution import empty_refs, refs_by_agent_ids
from autowonder.squads.models import Squad, SquadMember
from autowonder.workspaces.models import Org


def require_agent_name(name: str | None) -> str:
    """名称必填，并去掉两端空白。"""
    if name is None or name.strip() == "":
        raise BizError(ErrorCode.AGENT_NAME_REQUIRED)
    return name.strip()


def page_window(page: int, size: int) -> tuple[int, int]:
    """页码小于 1 时从第 1 页起；每页小于 1 时用 20，并且不超过 100。"""
    normalized_page = page
    if page < 1:
        normalized_page = 1
    normalized_size = size
    if size < 1:
        normalized_size = 20
    if normalized_size > 100:
        normalized_size = 100
    return (normalized_page - 1) * normalized_size, normalized_size


def field_provided(provided_fields: set[str] | None, field_name: str) -> bool:
    """字段集合为空时，每个字段都算提交过。"""
    if provided_fields is None:
        return True
    return field_name in provided_fields


def merge_field(
    provided_fields: set[str] | None,
    field_name: str,
    requested: str | None,
    current: str | None,
) -> str | None:
    """旧语义只在请求非空时覆盖；显式字段集合里的 null 会清空。"""
    if provided_fields is None:
        if requested is not None:
            return requested
        return current
    if field_name in provided_fields:
        return requested
    return current


def version_fields_present(
    provided_fields: set[str] | None,
    role_code: str | None,
    role_name: str | None,
    business_background: str | None,
    responsibilities: str | None,
) -> bool:
    """旧接口只有非空角色字段才改草稿；字段集合则看键是否出现。"""
    if provided_fields is None:
        if role_code is not None:
            return True
        if role_name is not None:
            return True
        if business_background is not None:
            return True
        if responsibilities is not None:
            return True
        return False
    if "roleCode" in provided_fields:
        return True
    if "roleName" in provided_fields:
        return True
    if "businessBackground" in provided_fields:
        return True
    if "responsibilities" in provided_fields:
        return True
    return False


def platform_profile_locked(
    kind: str,
    provided_fields: set[str] | None,
    name: str | None,
    avatar_url: str | None,
) -> bool:
    """平台数字员工不能改非空的名称或头像。"""
    if kind != "PLATFORM":
        return False
    if field_provided(provided_fields, "name") and name is not None:
        return True
    return field_provided(provided_fields, "avatarUrl") and avatar_url is not None


def platform_config_locked(kind: str, sdlc_id: int | None) -> bool:
    """平台数字员工不能改绑流程。清空流程不算修改。"""
    return kind == "PLATFORM" and sdlc_id is not None


def memory_ref_source(scope: str | None) -> str:
    """记忆范围写成导入来源；空白范围按直接挂载。"""
    if scope is None or scope.strip() == "":
        return "DIRECT"
    return scope + "_IMPORT"


async def create_agent(
    session: AsyncSession,
    request: CreateAgentRequest,
    tenant_id: int,
    user_id: int,
) -> AgentView:
    """创建草稿员工和 1 号草稿版本。响应不回读创建时间，版本号仍是写入前的 0。"""
    agent = Agent(
        tenant_id=tenant_id,
        name=require_agent_name(request.name),
        avatar_url=request.avatar_url,
        status="DRAFT",
        kind="STANDARD",
        latest_version_no=1,
        creator_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(agent)
    await session.flush()
    version = AgentVersion(
        tenant_id=tenant_id,
        agent_id=agent.id,
        version_no=1,
        status="DRAFT",
        role_name=request.role_name,
        role_code=request.role_code,
        business_background=request.business_background,
        responsibilities=request.responsibilities,
        creator_id=user_id,
        version=0,
        is_deleted=0,
    )
    session.add(version)
    await session.flush()
    view = _to_view(agent)
    view.editing_version_id = version.id
    view.gmt_create = None
    stored_version = agent.version
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        "DRAFT",
        None,
        version.id,
        1,
        stored_version,
        user_id,
    )
    _require_updated(updated)
    await session.commit()
    return view


async def get_agent(session: AsyncSession, agent_id: int, tenant_id: int) -> AgentView:
    """详情展示在线版本，不填小队。"""
    return await _to_summary(session, await _find_in_tenant(session, agent_id, tenant_id))


async def list_agents(
    session: AsyncSession,
    tenant_id: int,
    status: str | None,
    kind: str | None,
    squad_ids: list[int] | None,
    page: int,
    size: int,
) -> list[AgentView]:
    """分页列出数字员工，并填上所在小队。"""
    offset, limit = page_window(page, size)
    statement = select(Agent).where(Agent.is_deleted == 0, Agent.tenant_id == tenant_id)
    if status is not None:
        statement = statement.where(Agent.status == status)
    if kind is not None:
        statement = statement.where(Agent.kind == kind)
    if squad_ids is not None and len(squad_ids) > 0:
        member_ids = (
            select(SquadMember.agent_id)
            .join(Squad, and_(Squad.id == SquadMember.squad_id, Squad.is_deleted == 0))
            .where(SquadMember.squad_id.in_(squad_ids), SquadMember.tenant_id == tenant_id)
        )
        statement = statement.where(Agent.id.in_(member_ids))
    statement = statement.order_by(Agent.id.desc()).offset(offset).limit(limit)
    rows = list(await session.scalars(statement))
    views = [await _to_summary(session, agent) for agent in rows]
    await _fill_squads(session, tenant_id, views)
    return views


async def count_pending_reviews(session: AsyncSession, tenant_id: int) -> int:
    """待审核数字员工数量。"""
    counted = await session.scalar(
        select(func.count())
        .select_from(Agent)
        .where(
            Agent.tenant_id == tenant_id,
            Agent.status == "PENDING_REVIEW",
            Agent.is_deleted == 0,
        )
    )
    return cast(int, counted)


async def edit_config(
    session: AsyncSession,
    agent_id: int,
    request: UpdateConfigRequest,
    tenant_id: int,
    user_id: int,
    provided_fields: set[str] | None = None,
) -> AgentVersionView:
    """修改草稿配置。HTTP 调用不传字段集合，null 会清空对应列。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    if platform_config_locked(agent.kind, request.sdlc_id):
        raise BizError(ErrorCode.AGENT_PLATFORM_LOCKED)
    draft = await _ensure_draft(session, agent, tenant_id, user_id)
    identity = draft.identity_json
    write_identity = False
    if field_provided(provided_fields, "evolutionMode"):
        replaced = identity_with_evolution_mode(draft.identity_json, request.evolution_mode)
        if replaced is not None:
            identity = replaced
            write_identity = True
    elif identity is not None:
        write_identity = True
    role_name = draft.role_name
    if field_provided(provided_fields, "roleName"):
        role_name = request.role_name
    role_code = draft.role_code
    if field_provided(provided_fields, "roleCode"):
        role_code = request.role_code
    background = draft.business_background
    if field_provided(provided_fields, "businessBackground"):
        background = request.business_background
    responsibilities = draft.responsibilities
    if field_provided(provided_fields, "responsibilities"):
        responsibilities = request.responsibilities
    sdlc_id = draft.sdlc_id
    if field_provided(provided_fields, "sdlcId"):
        sdlc_id = request.sdlc_id
    stored_identity = None
    if write_identity:
        stored_identity = identity
    updated = await _update_version_config(
        session,
        draft.id,
        tenant_id,
        role_name,
        role_code,
        background,
        responsibilities,
        sdlc_id,
        stored_identity,
        write_identity,
        draft.version,
        user_id,
    )
    _require_updated(updated)
    await session.commit()
    session.expire_all()
    return await _to_version_view(session, await _require_version(session, draft.id))


async def update_agent(
    session: AsyncSession,
    agent_id: int,
    request: UpdateAgentRequest,
    tenant_id: int,
    user_id: int,
    provided_fields: set[str] | None = None,
) -> AgentView:
    """更新展示字段。角色文案有变化时才打开草稿。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    if platform_profile_locked(agent.kind, provided_fields, request.name, request.avatar_url):
        raise BizError(ErrorCode.AGENT_PLATFORM_LOCKED)
    if field_provided(provided_fields, "name") and request.name is not None:
        renamed = await _update_name(
            session,
            agent.id,
            tenant_id,
            require_agent_name(request.name),
            agent.version,
            user_id,
        )
        _require_updated(renamed)
        agent = await _reload_agent(session, agent.id)
    if field_provided(provided_fields, "avatarUrl") and request.avatar_url is not None:
        stored_avatar: str | None = request.avatar_url.strip()
        if stored_avatar == "":
            stored_avatar = None
        updated = await _update_avatar(
            session,
            agent.id,
            tenant_id,
            stored_avatar,
            agent.version,
            user_id,
        )
        _require_updated(updated)
        agent = await _reload_agent(session, agent.id)
    if version_fields_present(
        provided_fields,
        request.role_code,
        request.role_name,
        request.business_background,
        request.responsibilities,
    ):
        draft = await _ensure_draft(session, agent, tenant_id, user_id)
        updated = await _update_version_config(
            session,
            draft.id,
            tenant_id,
            merge_field(provided_fields, "roleName", request.role_name, draft.role_name),
            merge_field(provided_fields, "roleCode", request.role_code, draft.role_code),
            merge_field(
                provided_fields,
                "businessBackground",
                request.business_background,
                draft.business_background,
            ),
            merge_field(
                provided_fields,
                "responsibilities",
                request.responsibilities,
                draft.responsibilities,
            ),
            draft.sdlc_id,
            draft.identity_json,
            draft.identity_json is not None,
            draft.version,
            user_id,
        )
        _require_updated(updated)
    await session.commit()
    session.expire_all()
    return await _to_summary(session, await _require_agent(session, agent_id))


async def submit_agent(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    user_id: int,
) -> AgentView:
    """把当前草稿送审，并补上适用记忆。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    draft = await _require_editing_draft(session, agent, tenant_id)
    await _reconcile_memories(session, agent_id, tenant_id, draft)
    updated = await _update_version_status(
        session,
        draft.id,
        tenant_id,
        "PENDING_REVIEW",
        None,
        None,
        None,
        False,
        draft.version,
        user_id,
    )
    _require_updated(updated)
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        "PENDING_REVIEW",
        agent.online_version_id,
        agent.editing_version_id,
        agent.latest_version_no,
        agent.version,
        user_id,
    )
    _require_updated(updated)
    await session.commit()
    session.expire_all()
    return await _to_summary(session, await _require_agent(session, agent_id))


async def approve_agent(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    user_id: int,
    comment: str | None,
) -> AgentView:
    """通过审核并切到该版本。作者本人需要空间所有者或管理员。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    pending = await _require_pending(session, agent, tenant_id)
    if pending.creator_id is not None and pending.creator_id == user_id:
        if not await _can_approve_own(session, tenant_id, user_id):
            raise BizError(ErrorCode.NO_PERMISSION)
    await _validate_environment_refs(session, tenant_id, pending.id)
    identity = build_identity_map(
        name=agent.name,
        avatar_url=agent.avatar_url,
        role_name=pending.role_name,
        role_code=pending.role_code,
        business_background=pending.business_background,
        responsibilities=pending.responsibilities,
        identity=pending.identity_json,
    )
    updated = await _update_version_status(
        session,
        pending.id,
        tenant_id,
        "APPROVED",
        user_id,
        comment,
        identity,
        True,
        pending.version,
        user_id,
    )
    _require_updated(updated)
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        "ONLINE",
        pending.id,
        None,
        agent.latest_version_no,
        agent.version,
        user_id,
    )
    _require_updated(updated)
    await session.commit()
    session.expire_all()
    return await _to_summary(session, await _require_agent(session, agent_id))


async def reject_agent(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    user_id: int,
    comment: str | None,
) -> AgentView:
    """驳回后回到在线版本；没有在线版本则回到草稿。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    pending = await _require_pending(session, agent, tenant_id)
    updated = await _update_version_status(
        session,
        pending.id,
        tenant_id,
        "REJECTED",
        user_id,
        comment,
        None,
        False,
        pending.version,
        user_id,
    )
    _require_updated(updated)
    status = "DRAFT"
    if agent.online_version_id is not None:
        status = "ONLINE"
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        status,
        agent.online_version_id,
        None,
        agent.latest_version_no,
        agent.version,
        user_id,
    )
    _require_updated(updated)
    await session.commit()
    session.expire_all()
    return await _to_summary(session, await _require_agent(session, agent_id))


async def rollback_agent(
    session: AsyncSession,
    agent_id: int,
    target_version_no: int | None,
    tenant_id: int,
    user_id: int,
) -> AgentView:
    """把在线指针拨回一个已通过版本，草稿指针保持不动。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    target = await _find_version_by_no(session, agent_id, target_version_no)
    if target is None or not _version_for_agent(target, agent, tenant_id):
        raise BizError(ErrorCode.AGENT_ROLLBACK_TARGET_INVALID)
    if target.status != "APPROVED":
        raise BizError(ErrorCode.AGENT_ROLLBACK_TARGET_INVALID)
    await _validate_environment_refs(session, tenant_id, target.id)
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        "ONLINE",
        target.id,
        agent.editing_version_id,
        agent.latest_version_no,
        agent.version,
        user_id,
    )
    _require_updated(updated)
    await session.commit()
    session.expire_all()
    return await _to_summary(session, await _require_agent(session, agent_id))


async def offline_agent(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    user_id: int,
) -> AgentView:
    """下线并清空在线版本。平台数字员工不能下线。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    if agent.kind == "PLATFORM":
        raise BizError(ErrorCode.AGENT_PLATFORM_NO_OFFLINE)
    if agent.status != "ONLINE":
        raise BizError(ErrorCode.AGENT_NOT_ONLINE)
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        "OFFLINE",
        None,
        agent.editing_version_id,
        agent.latest_version_no,
        agent.version,
        user_id,
    )
    _require_updated(updated)
    await session.commit()
    session.expire_all()
    return await _to_summary(session, await _require_agent(session, agent_id))


async def online_agent(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    user_id: int,
) -> AgentView:
    """用最新的已通过版本重新上线。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    if agent.status != "OFFLINE":
        raise BizError(ErrorCode.AGENT_NOT_OFFLINE)
    approved = await _list_approved(session, agent_id)
    if len(approved) == 0:
        raise BizError(ErrorCode.AGENT_ONLINE_NO_APPROVED_VERSION)
    target = approved[0]
    if not _version_for_agent(target, agent, tenant_id):
        raise BizError(ErrorCode.AGENT_ONLINE_NO_APPROVED_VERSION)
    await _validate_environment_refs(session, tenant_id, target.id)
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        "ONLINE",
        target.id,
        agent.editing_version_id,
        agent.latest_version_no,
        agent.version,
        user_id,
    )
    _require_updated(updated)
    await session.commit()
    session.expire_all()
    return await _to_summary(session, await _require_agent(session, agent_id))


async def delete_agent(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """先清版本子表，再软删除版本和员工。在线员工不能删。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    if agent.kind == "PLATFORM":
        raise BizError(ErrorCode.AGENT_PLATFORM_NO_DELETE)
    if agent.status == "ONLINE":
        raise BizError(ErrorCode.AGENT_ONLINE_NO_DELETE)
    for version in await _list_versions(session, agent_id):
        await session.execute(delete(AgentSkill).where(AgentSkill.agent_version_id == version.id))
        await session.execute(
            delete(AgentRepoPerm).where(AgentRepoPerm.agent_version_id == version.id)
        )
        await session.execute(
            delete(AgentMemoryRef).where(AgentMemoryRef.agent_version_id == version.id)
        )
        await session.execute(
            delete(AgentEnvironmentVariableRef).where(
                AgentEnvironmentVariableRef.tenant_id == tenant_id,
                AgentEnvironmentVariableRef.agent_version_id == version.id,
            )
        )
    await session.execute(
        update(AgentVersion)
        .where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.tenant_id == tenant_id,
            AgentVersion.is_deleted == 0,
        )
        .values(is_deleted=1, version=AgentVersion.version + 1, modifier_id=user_id)
    )
    deleted = rowcount(
        await session.execute(
            update(Agent)
            .where(
                Agent.id == agent_id,
                Agent.tenant_id == tenant_id,
                Agent.version == agent.version,
                Agent.is_deleted == 0,
            )
            .values(is_deleted=1, version=Agent.version + 1, modifier_id=user_id)
        )
    )
    _require_updated(deleted)
    await session.commit()


async def list_versions(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
) -> list[AgentVersionSummaryView]:
    """版本号从新到旧。"""
    await _find_in_tenant(session, agent_id, tenant_id)
    views: list[AgentVersionSummaryView] = []
    for version in await _list_versions(session, agent_id):
        views.append(
            AgentVersionSummaryView(
                id=version.id,
                version_no=version.version_no,
                status=version.status,
                role_name=version.role_name,
                gmt_create=version.gmt_create,
            )
        )
    return views


async def get_version(
    session: AsyncSession,
    agent_id: int,
    version_no: int,
    tenant_id: int,
) -> AgentVersionView:
    """读取某个版本号的完整配置。"""
    await _find_in_tenant(session, agent_id, tenant_id)
    version = await _find_version_by_no(session, agent_id, version_no)
    if version is None:
        raise BizError(ErrorCode.AGENT_VERSION_NOT_FOUND)
    return await _to_version_view(session, version)


async def add_repo_perm(
    session: AsyncSession,
    agent_id: int,
    request: RepoPermRequest,
    tenant_id: int,
    user_id: int,
) -> None:
    """给草稿合并仓库权限。并发插入撞唯一键时改为锁定后合并。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    if agent.kind == "PLATFORM":
        raise BizError(ErrorCode.AGENT_PLATFORM_REPO_LOCKED)
    allowed = None
    if request.allowed_branch_patterns is not None:
        allowed = encode(request.allowed_branch_patterns)
    draft = await _ensure_draft(session, agent, tenant_id, user_id)
    existing = await _find_repo_perm(session, draft.id, request.repo_id)
    if existing is not None:
        _merge_repo_perm(existing, request, allowed, tenant_id, draft.id)
        await _update_repo_perm(session, existing)
        await session.commit()
        return
    perm_level = request.perm_level
    if perm_level is None:
        perm_level = "READ"
    perm = AgentRepoPerm(
        tenant_id=tenant_id,
        agent_version_id=draft.id,
        repo_id=request.repo_id,
        perm_level=perm_level,
        allowed_branch_patterns=allowed,
    )
    try:
        async with session.begin_nested():
            session.add(perm)
            await session.flush()
    except IntegrityError as error:
        if not _duplicate_key(error):
            raise
        winner = await _lock_repo_perm(session, draft.id, request.repo_id, tenant_id)
        if winner is None:
            raise
        _merge_repo_perm(winner, request, allowed, tenant_id, draft.id)
        await _update_repo_perm(session, winner)
    await session.commit()


async def remove_repo_perm(
    session: AsyncSession,
    agent_id: int,
    repo_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """从草稿移除仓库。没有对应行也算成功。"""
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    if agent.kind == "PLATFORM":
        raise BizError(ErrorCode.AGENT_PLATFORM_REPO_LOCKED)
    draft = await _ensure_draft(session, agent, tenant_id, user_id)
    await session.execute(
        delete(AgentRepoPerm).where(
            AgentRepoPerm.agent_version_id == draft.id,
            AgentRepoPerm.repo_id == repo_id,
            AgentRepoPerm.tenant_id == tenant_id,
        )
    )
    await session.commit()


async def add_skill(
    session: AsyncSession,
    agent_id: int,
    request: SkillRequest | None,
    tenant_id: int,
    user_id: int,
) -> None:
    """把技能挂到草稿。已挂载则直接返回。"""
    if request is None or request.skill_id is None:
        raise BizError(ErrorCode.SKILL_NOT_FOUND)
    draft = await _ensure_draft_for_edit(session, agent_id, tenant_id, user_id)
    skill = await session.scalar(
        select(Skill).where(Skill.id == request.skill_id, Skill.is_deleted == 0).limit(1)
    )
    if skill is None or skill.tenant_id != tenant_id:
        raise BizError(ErrorCode.SKILL_NOT_FOUND)
    mounted = await _list_skills(session, draft.id)
    for item in mounted:
        if item.skill_id == request.skill_id:
            await session.commit()
            return
    session.add(
        AgentSkill(
            tenant_id=tenant_id,
            agent_version_id=draft.id,
            skill_id=request.skill_id,
        )
    )
    await session.commit()


async def remove_skill(
    session: AsyncSession,
    agent_id: int,
    skill_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """从草稿移除技能。"""
    draft = await _ensure_draft_for_edit(session, agent_id, tenant_id, user_id)
    await session.execute(
        delete(AgentSkill).where(
            AgentSkill.agent_version_id == draft.id,
            AgentSkill.skill_id == skill_id,
            AgentSkill.tenant_id == tenant_id,
        )
    )
    await session.commit()


async def add_memory_ref(
    session: AsyncSession,
    agent_id: int,
    request: MemoryRefRequest,
    tenant_id: int,
    user_id: int,
) -> None:
    """挂载记忆。已存在则直接返回。"""
    draft = await _ensure_draft_for_edit(session, agent_id, tenant_id, user_id)
    if await _memory_exists(session, draft.id, request.memory_id, tenant_id):
        await session.commit()
        return
    source = "DIRECT"
    if request.source is not None:
        source = request.source
    session.add(
        AgentMemoryRef(
            tenant_id=tenant_id,
            agent_version_id=draft.id,
            memory_id=request.memory_id,
            source=source,
        )
    )
    await session.commit()


async def remove_memory_ref(
    session: AsyncSession,
    agent_id: int,
    memory_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """从草稿移除记忆。"""
    draft = await _ensure_draft_for_edit(session, agent_id, tenant_id, user_id)
    await session.execute(
        delete(AgentMemoryRef).where(
            AgentMemoryRef.agent_version_id == draft.id,
            AgentMemoryRef.memory_id == memory_id,
            AgentMemoryRef.tenant_id == tenant_id,
        )
    )
    await session.commit()


async def list_memory_refs(session: AsyncSession, agent_id: int) -> list[MemoryRefItem]:
    """在线版本优先，否则看编辑中的版本。"""
    agent = await _find_agent_row(session, agent_id)
    if agent is None:
        raise BizError(ErrorCode.AGENT_NOT_FOUND)
    version_id = agent.online_version_id
    if version_id is None:
        version_id = agent.editing_version_id
    if version_id is None:
        return []
    return [
        MemoryRefItem(memory_id=item.memory_id, source=item.source)
        for item in await _list_memories(session, version_id)
    ]


async def add_environment_variable_ref(
    session: AsyncSession,
    agent_id: int,
    environment_variable_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """把仍有效的环境变量挂到草稿。重复挂载按成功处理。"""
    draft = await _ensure_draft_for_edit(session, agent_id, tenant_id, user_id)
    variable = await _lock_variable(session, tenant_id, environment_variable_id)
    if variable is None:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_NOT_FOUND)
    if await _env_ref_exists(session, tenant_id, draft.id, environment_variable_id):
        await session.commit()
        return
    ref = AgentEnvironmentVariableRef(
        tenant_id=tenant_id,
        agent_version_id=draft.id,
        environment_variable_id=environment_variable_id,
    )
    try:
        async with session.begin_nested():
            session.add(ref)
            await session.flush()
    except IntegrityError as error:
        if not _duplicate_key(error):
            raise
    await session.commit()


async def remove_environment_variable_ref(
    session: AsyncSession,
    agent_id: int,
    environment_variable_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """从草稿解绑环境变量。"""
    draft = await _ensure_draft_for_edit(session, agent_id, tenant_id, user_id)
    await session.execute(
        delete(AgentEnvironmentVariableRef).where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.agent_version_id == draft.id,
            AgentEnvironmentVariableRef.environment_variable_id == environment_variable_id,
        )
    )
    await session.commit()


async def list_environment_variable_refs(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
) -> list[EnvironmentVariableRefView]:
    """在线版本优先列出脱敏后的环境变量。"""
    agent = await _find_in_tenant(session, agent_id, tenant_id)
    version_id = agent.online_version_id
    if version_id is None:
        version_id = agent.editing_version_id
    if version_id is None:
        return []
    return await _list_env_metadata(session, tenant_id, version_id)


async def attach_reviewed_memory(
    session: AsyncSession,
    agent_id: int,
    memory_id: int,
    source: str | None,
    tenant_id: int,
    user_id: int,
) -> None:
    """把已采纳记忆挂到在线员工。草稿会因此送审。

    员工不存在时安静返回。写入留在调用方的事务里，和记忆审核一起提交。
    """
    try:
        agent = await _lock_in_tenant(session, agent_id, tenant_id)
    except BizError:
        return
    if agent.online_version_id is None:
        return
    target = await _editable_version(session, agent, tenant_id)
    if target is None:
        target = await _ensure_draft(session, agent, tenant_id, user_id)
        agent = await _reload_agent(session, agent_id)
    if not await _memory_exists(session, target.id, memory_id, tenant_id):
        session.add(
            AgentMemoryRef(
                tenant_id=tenant_id,
                agent_version_id=target.id,
                memory_id=memory_id,
                source=memory_ref_source(source),
            )
        )
        await session.flush()
    if target.status != "DRAFT":
        return
    updated = await _update_version_status(
        session,
        target.id,
        tenant_id,
        "PENDING_REVIEW",
        None,
        "系统自动提交：自动同步已采纳记忆 #" + str(memory_id),
        None,
        False,
        target.version,
        user_id,
    )
    _require_updated(updated)
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        "PENDING_REVIEW",
        agent.online_version_id,
        target.id,
        agent.latest_version_no,
        agent.version,
        user_id,
    )
    _require_updated(updated)


def _require_updated(updated: int) -> None:
    if updated == 0:
        raise BizError(ErrorCode.AGENT_VERSION_CONFLICT)


def _version_for_agent(version: AgentVersion | None, agent: Agent, tenant_id: int) -> bool:
    if version is None:
        return False
    if version.tenant_id != tenant_id:
        return False
    return version.agent_id == agent.id


def _merge_repo_perm(
    permission: AgentRepoPerm,
    request: RepoPermRequest,
    allowed: str | None,
    tenant_id: int,
    version_id: int,
) -> None:
    permission.tenant_id = tenant_id
    permission.agent_version_id = version_id
    if request.perm_level is not None:
        permission.perm_level = request.perm_level
    if request.allowed_branch_patterns is not None:
        permission.allowed_branch_patterns = allowed


async def _update_repo_perm(session: AsyncSession, permission: AgentRepoPerm) -> None:
    updated = rowcount(
        await session.execute(
            update(AgentRepoPerm)
            .where(
                AgentRepoPerm.tenant_id == permission.tenant_id,
                AgentRepoPerm.agent_version_id == permission.agent_version_id,
                AgentRepoPerm.repo_id == permission.repo_id,
            )
            .values(
                perm_level=permission.perm_level,
                allowed_branch_patterns=permission.allowed_branch_patterns,
            )
        )
    )
    if updated != 1:
        raise BizError(ErrorCode.CONFLICT, "仓库权限已被修改，请刷新后重试")


async def _ensure_draft_for_edit(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    user_id: int,
) -> AgentVersion:
    agent = await _lock_in_tenant(session, agent_id, tenant_id)
    return await _ensure_draft(session, agent, tenant_id, user_id)


async def _ensure_draft(
    session: AsyncSession,
    agent: Agent,
    tenant_id: int,
    user_id: int,
) -> AgentVersion:
    """已有草稿直接用。待审核版本挡住编辑。没有草稿时从在线版本克隆。"""
    if agent.editing_version_id is not None:
        existing = await _find_version(session, agent.editing_version_id)
        if (
            existing is not None
            and _version_for_agent(existing, agent, tenant_id)
            and existing.status == "DRAFT"
        ):
            return existing
        raise BizError(ErrorCode.AGENT_NOT_DRAFT)
    source = None
    if agent.online_version_id is not None:
        source = await _find_version(session, agent.online_version_id)
    new_no = agent.latest_version_no + 1
    draft = AgentVersion(
        tenant_id=tenant_id,
        agent_id=agent.id,
        version_no=new_no,
        status="DRAFT",
        version=0,
        creator_id=user_id,
        is_deleted=0,
    )
    if source is not None:
        draft.role_name = source.role_name
        draft.role_code = source.role_code
        draft.business_background = source.business_background
        draft.responsibilities = source.responsibilities
        draft.sdlc_id = source.sdlc_id
        if isinstance(source.identity_json, dict):
            draft.identity_json = dict(source.identity_json)
        else:
            draft.identity_json = source.identity_json
    session.add(draft)
    await session.flush()
    if source is not None:
        await _clone_subtables(session, source.id, draft.id, tenant_id)
    stored_version = agent.version
    updated = await _update_agent_status(
        session,
        agent.id,
        tenant_id,
        agent.status,
        agent.online_version_id,
        draft.id,
        new_no,
        stored_version,
        user_id,
    )
    _require_updated(updated)
    return draft


async def _clone_subtables(
    session: AsyncSession,
    source_version_id: int,
    target_version_id: int,
    tenant_id: int,
) -> None:
    for perm in await _list_repo_perms(session, source_version_id):
        session.add(
            AgentRepoPerm(
                tenant_id=tenant_id,
                agent_version_id=target_version_id,
                repo_id=perm.repo_id,
                perm_level=perm.perm_level,
                allowed_branch_patterns=perm.allowed_branch_patterns,
            )
        )
    for skill in await _list_skills(session, source_version_id):
        session.add(
            AgentSkill(
                tenant_id=tenant_id,
                agent_version_id=target_version_id,
                skill_id=skill.skill_id,
            )
        )
    for memory in await _list_memories(session, source_version_id):
        session.add(
            AgentMemoryRef(
                tenant_id=tenant_id,
                agent_version_id=target_version_id,
                memory_id=memory.memory_id,
                source=memory.source,
            )
        )
    for ref in await _list_env_refs(session, tenant_id, source_version_id):
        session.add(
            AgentEnvironmentVariableRef(
                tenant_id=tenant_id,
                agent_version_id=target_version_id,
                environment_variable_id=ref.environment_variable_id,
            )
        )
    await session.flush()


async def _lock_in_tenant(session: AsyncSession, agent_id: int, tenant_id: int) -> Agent:
    """先锁员工行，再读状态。后续审核和子表修改都从这里进入。"""
    await session.execute(
        select(Agent.id)
        .where(Agent.tenant_id == tenant_id, Agent.id == agent_id, Agent.is_deleted == 0)
        .with_for_update()
    )
    return await _find_in_tenant(session, agent_id, tenant_id)


async def _find_in_tenant(session: AsyncSession, agent_id: int, tenant_id: int) -> Agent:
    agent = await _find_agent_row(session, agent_id)
    if agent is None or agent.tenant_id != tenant_id:
        raise BizError(ErrorCode.AGENT_NOT_FOUND)
    return agent


async def _find_agent_row(session: AsyncSession, agent_id: int) -> Agent | None:
    return await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted == 0).limit(1)
    )


async def _require_agent(session: AsyncSession, agent_id: int) -> Agent:
    agent = await _find_agent_row(session, agent_id)
    if agent is None:
        raise BizError(ErrorCode.AGENT_NOT_FOUND)
    return agent


async def _reload_agent(session: AsyncSession, agent_id: int) -> Agent:
    session.expire_all()
    return await _require_agent(session, agent_id)


async def _require_editing_draft(
    session: AsyncSession,
    agent: Agent,
    tenant_id: int,
) -> AgentVersion:
    if agent.editing_version_id is None:
        raise BizError(ErrorCode.AGENT_NOT_DRAFT)
    draft = await _find_version(session, agent.editing_version_id)
    if draft is None or not _version_for_agent(draft, agent, tenant_id):
        raise BizError(ErrorCode.AGENT_NOT_DRAFT)
    if draft.status != "DRAFT":
        raise BizError(ErrorCode.AGENT_NOT_DRAFT)
    return draft


async def _require_pending(
    session: AsyncSession,
    agent: Agent,
    tenant_id: int,
) -> AgentVersion:
    if agent.editing_version_id is None:
        raise BizError(ErrorCode.AGENT_NOT_PENDING)
    pending = await _find_version(session, agent.editing_version_id)
    if pending is None or not _version_for_agent(pending, agent, tenant_id):
        raise BizError(ErrorCode.AGENT_NOT_PENDING)
    if pending.status != "PENDING_REVIEW":
        raise BizError(ErrorCode.AGENT_NOT_PENDING)
    return pending


async def _editable_version(
    session: AsyncSession,
    agent: Agent,
    tenant_id: int,
) -> AgentVersion | None:
    if agent.editing_version_id is None:
        return None
    editing = await _find_version(session, agent.editing_version_id)
    if editing is None or not _version_for_agent(editing, agent, tenant_id):
        return None
    if editing.status == "DRAFT" or editing.status == "PENDING_REVIEW":
        return editing
    return None


async def _can_approve_own(session: AsyncSession, tenant_id: int, user_id: int) -> bool:
    workspace = await session.scalar(
        select(Org).where(Org.id == tenant_id, Org.is_deleted == 0).limit(1)
    )
    if workspace is not None and workspace.owner_id == user_id:
        return True
    context = current()
    if context.workspace_id != tenant_id or context.user_id != user_id:
        return False
    return context.access_level == WorkspaceAccessLevel.ADMIN.name


async def _validate_environment_refs(
    session: AsyncSession,
    tenant_id: int,
    version_id: int,
) -> None:
    invalid = await _count_invalid_env_refs(session, tenant_id, version_id)
    if invalid > 0:
        raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_REFERENCE_INVALID)
    for ref in await _list_env_refs(session, tenant_id, version_id):
        variable = await _lock_variable(session, tenant_id, ref.environment_variable_id)
        if variable is None:
            raise BizError(ErrorCode.ENVIRONMENT_VARIABLE_REFERENCE_INVALID)


async def _reconcile_memories(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    draft: AgentVersion,
) -> None:
    for memory in await _list_applicable_memories(session, tenant_id, agent_id):
        if await _memory_exists(session, draft.id, memory.id, tenant_id):
            continue
        session.add(
            AgentMemoryRef(
                tenant_id=tenant_id,
                agent_version_id=draft.id,
                memory_id=memory.id,
                source=memory_ref_source(memory.scope),
            )
        )
    await session.flush()


async def _list_applicable_memories(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
) -> list[Memory]:
    member_match = (
        select(SquadMember.id)
        .where(
            SquadMember.tenant_id == tenant_id,
            SquadMember.squad_id == Memory.owner_ref,
            SquadMember.agent_id == agent_id,
        )
        .exists()
    )
    rows = await session.scalars(
        select(Memory)
        .where(
            Memory.tenant_id == tenant_id,
            Memory.status == "ADOPTED",
            Memory.is_deleted == 0,
            or_(
                Memory.scope == "ORG",
                and_(Memory.scope == "AGENT", Memory.owner_ref == agent_id),
                and_(Memory.scope == "SQUAD", member_match),
            ),
        )
        .order_by(Memory.id.asc())
    )
    return list(rows)


async def _update_agent_status(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    status: str,
    online_version_id: int | None,
    editing_version_id: int | None,
    latest_version_no: int,
    version: int,
    user_id: int,
) -> int:
    return rowcount(
        await session.execute(
            update(Agent)
            .where(
                Agent.id == agent_id,
                Agent.tenant_id == tenant_id,
                Agent.version == version,
                Agent.is_deleted == 0,
            )
            .values(
                status=status,
                online_version_id=online_version_id,
                editing_version_id=editing_version_id,
                latest_version_no=latest_version_no,
                version=Agent.version + 1,
                modifier_id=user_id,
            )
        )
    )


async def _update_name(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    name: str,
    version: int,
    user_id: int,
) -> int:
    return rowcount(
        await session.execute(
            update(Agent)
            .where(
                Agent.id == agent_id,
                Agent.tenant_id == tenant_id,
                Agent.version == version,
                Agent.is_deleted == 0,
            )
            .values(name=name, version=Agent.version + 1, modifier_id=user_id)
        )
    )


async def _update_avatar(
    session: AsyncSession,
    agent_id: int,
    tenant_id: int,
    avatar_url: str | None,
    version: int,
    user_id: int,
) -> int:
    return rowcount(
        await session.execute(
            update(Agent)
            .where(
                Agent.id == agent_id,
                Agent.tenant_id == tenant_id,
                Agent.version == version,
                Agent.is_deleted == 0,
            )
            .values(avatar_url=avatar_url, version=Agent.version + 1, modifier_id=user_id)
        )
    )


async def _update_version_config(
    session: AsyncSession,
    version_id: int,
    tenant_id: int,
    role_name: str | None,
    role_code: str | None,
    business_background: str | None,
    responsibilities: str | None,
    sdlc_id: int | None,
    identity: object | None,
    write_identity: bool,
    version: int,
    user_id: int,
) -> int:
    values: dict[str, object] = {
        "role_name": role_name,
        "role_code": role_code,
        "business_background": business_background,
        "responsibilities": responsibilities,
        "sdlc_id": sdlc_id,
        "version": AgentVersion.version + 1,
        "modifier_id": user_id,
    }
    if write_identity:
        values["identity_json"] = identity
    return rowcount(
        await session.execute(
            update(AgentVersion)
            .where(
                AgentVersion.id == version_id,
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.version == version,
                AgentVersion.is_deleted == 0,
            )
            .values(**values)
        )
    )


async def _update_version_status(
    session: AsyncSession,
    version_id: int,
    tenant_id: int,
    status: str,
    reviewer_id: int | None,
    review_comment: str | None,
    identity: object | None,
    write_identity: bool,
    version: int,
    user_id: int,
) -> int:
    values: dict[str, object] = {
        "status": status,
        "reviewer_id": reviewer_id,
        "review_comment": review_comment,
        "version": AgentVersion.version + 1,
        "modifier_id": user_id,
    }
    if write_identity:
        values["identity_json"] = identity
    if reviewer_id is not None:
        values["reviewed_at"] = literal_column("NOW(3)")
    return rowcount(
        await session.execute(
            update(AgentVersion)
            .where(
                AgentVersion.id == version_id,
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.version == version,
                AgentVersion.is_deleted == 0,
            )
            .values(**values)
        )
    )


async def _find_version(session: AsyncSession, version_id: int) -> AgentVersion | None:
    return await session.scalar(
        select(AgentVersion)
        .where(AgentVersion.id == version_id, AgentVersion.is_deleted == 0)
        .limit(1)
    )


async def _require_version(session: AsyncSession, version_id: int) -> AgentVersion:
    version = await _find_version(session, version_id)
    if version is None:
        raise BizError(ErrorCode.AGENT_VERSION_NOT_FOUND)
    return version


async def _find_version_by_no(
    session: AsyncSession,
    agent_id: int,
    version_no: int | None,
) -> AgentVersion | None:
    return await session.scalar(
        select(AgentVersion)
        .where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.version_no == version_no,
            AgentVersion.is_deleted == 0,
        )
        .limit(1)
    )


async def _list_versions(session: AsyncSession, agent_id: int) -> list[AgentVersion]:
    rows = await session.scalars(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent_id, AgentVersion.is_deleted == 0)
        .order_by(AgentVersion.version_no.desc())
    )
    return list(rows)


async def _list_approved(session: AsyncSession, agent_id: int) -> list[AgentVersion]:
    rows = await session.scalars(
        select(AgentVersion)
        .where(
            AgentVersion.agent_id == agent_id,
            AgentVersion.status == "APPROVED",
            AgentVersion.is_deleted == 0,
        )
        .order_by(AgentVersion.version_no.desc())
    )
    return list(rows)


async def _resolve_display(session: AsyncSession, agent: Agent) -> AgentVersion | None:
    if agent.online_version_id is not None:
        online = await _find_version(session, agent.online_version_id)
        if online is not None:
            return online
    approved = await _list_approved(session, agent.id)
    if len(approved) > 0:
        return approved[0]
    if agent.editing_version_id is None:
        return None
    return await _find_version(session, agent.editing_version_id)


async def _find_draft(session: AsyncSession, agent: Agent) -> AgentVersion | None:
    if agent.editing_version_id is None:
        return None
    editing = await _find_version(session, agent.editing_version_id)
    if editing is not None and editing.status == "DRAFT":
        return editing
    return None


async def _to_summary(session: AsyncSession, agent: Agent) -> AgentView:
    view = _to_view(agent)
    display = await _resolve_display(session, agent)
    if display is not None:
        view.role_name = display.role_name
        view.role_code = display.role_code
        view.business_background = display.business_background
        view.responsibilities = display.responsibilities
        view.sdlc_id = display.sdlc_id
        view.evolution_mode = evolution_mode_of_identity(display.identity_json)
        view.repo_perm_count = len(await _list_repo_perms(session, display.id))
        view.skill_count = len(await _list_skills(session, display.id))
        view.memory_count = len(await _list_memories(session, display.id))
        view.environment_variables = await _list_env_metadata(session, agent.tenant_id, display.id)
    draft = await _find_draft(session, agent)
    view.has_draft = draft is not None
    if draft is None:
        view.draft_version_no = None
    else:
        view.draft_version_no = draft.version_no
    executors = await _list_executors(session, agent.tenant_id, agent.id)
    view.executor_total_count = len(executors)
    online = 0
    for executor in executors:
        if is_online(executor.id):
            online = online + 1
    view.executor_online_count = online
    return view


def _to_view(agent: Agent) -> AgentView:
    return AgentView(
        id=agent.id,
        name=agent.name,
        avatar_url=agent.avatar_url,
        kind=agent.kind,
        status=agent.status,
        online_version_id=agent.online_version_id,
        editing_version_id=agent.editing_version_id,
        latest_version_no=agent.latest_version_no,
        version=agent.version,
        gmt_create=agent.gmt_create,
    )


async def _to_version_view(session: AsyncSession, version: AgentVersion) -> AgentVersionView:
    repo_perms = [
        RepoPermItem(
            repo_id=perm.repo_id,
            perm_level=perm.perm_level,
            allowed_branch_patterns=decode(perm.allowed_branch_patterns),
        )
        for perm in await _list_repo_perms(session, version.id)
    ]
    skill_rows = await _list_skills(session, version.id)
    skills = [SkillItem(skill_id=item.skill_id) for item in skill_rows]
    memories = [
        MemoryRefItem(memory_id=item.memory_id, source=item.source)
        for item in await _list_memories(session, version.id)
    ]
    return AgentVersionView(
        id=version.id,
        agent_id=version.agent_id,
        version_no=version.version_no,
        status=version.status,
        role_name=version.role_name,
        role_code=version.role_code,
        business_background=version.business_background,
        responsibilities=version.responsibilities,
        sdlc_id=version.sdlc_id,
        identity_json=identity_column_text(version.identity_json),
        evolution_mode=evolution_mode_of_identity(version.identity_json),
        reviewer_id=version.reviewer_id,
        review_comment=version.review_comment,
        reviewed_at=version.reviewed_at,
        version=version.version,
        gmt_create=version.gmt_create,
        repo_perms=repo_perms,
        skills=skills,
        memory_refs=memories,
        environment_variables=await _list_env_metadata(session, version.tenant_id, version.id),
    )


async def _fill_squads(session: AsyncSession, tenant_id: int, views: list[AgentView]) -> None:
    refs = await refs_by_agent_ids(session, tenant_id, [view.id for view in views])
    for view in views:
        matched = empty_refs()
        if view.id is not None:
            matched = refs.get(view.id, empty_refs())
        view.squad_ids = list(matched.ids)
        view.squad_names = list(matched.names)


async def _list_repo_perms(session: AsyncSession, version_id: int) -> list[AgentRepoPerm]:
    rows = await session.scalars(
        select(AgentRepoPerm).where(AgentRepoPerm.agent_version_id == version_id)
    )
    return list(rows)


async def _find_repo_perm(
    session: AsyncSession,
    version_id: int,
    repo_id: int | None,
) -> AgentRepoPerm | None:
    for perm in await _list_repo_perms(session, version_id):
        if perm.repo_id == repo_id:
            return perm
    return None


async def _lock_repo_perm(
    session: AsyncSession,
    version_id: int,
    repo_id: int | None,
    tenant_id: int,
) -> AgentRepoPerm | None:
    return await session.scalar(
        select(AgentRepoPerm)
        .where(
            AgentRepoPerm.tenant_id == tenant_id,
            AgentRepoPerm.agent_version_id == version_id,
            AgentRepoPerm.repo_id == repo_id,
        )
        .with_for_update()
        .limit(1)
    )


async def _list_skills(session: AsyncSession, version_id: int) -> list[AgentSkill]:
    rows = await session.scalars(
        select(AgentSkill).where(AgentSkill.agent_version_id == version_id)
    )
    return list(rows)


async def _list_memories(session: AsyncSession, version_id: int) -> list[AgentMemoryRef]:
    rows = await session.scalars(
        select(AgentMemoryRef).where(AgentMemoryRef.agent_version_id == version_id)
    )
    return list(rows)


async def _memory_exists(
    session: AsyncSession,
    version_id: int,
    memory_id: int | None,
    tenant_id: int,
) -> bool:
    counted = await session.scalar(
        select(func.count())
        .select_from(AgentMemoryRef)
        .where(
            AgentMemoryRef.agent_version_id == version_id,
            AgentMemoryRef.memory_id == memory_id,
            AgentMemoryRef.tenant_id == tenant_id,
        )
    )
    return cast(int, counted) > 0


async def _list_env_refs(
    session: AsyncSession,
    tenant_id: int,
    version_id: int,
) -> list[AgentEnvironmentVariableRef]:
    rows = await session.scalars(
        select(AgentEnvironmentVariableRef)
        .where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.agent_version_id == version_id,
        )
        .order_by(
            AgentEnvironmentVariableRef.environment_variable_id.asc(),
            AgentEnvironmentVariableRef.id.asc(),
        )
    )
    return list(rows)


async def _list_env_metadata(
    session: AsyncSession,
    tenant_id: int,
    version_id: int,
) -> list[EnvironmentVariableRefView]:
    rows = await session.execute(
        select(EnvironmentVariable.id, EnvironmentVariable.name, EnvironmentVariable.description)
        .join(
            AgentEnvironmentVariableRef,
            AgentEnvironmentVariableRef.environment_variable_id == EnvironmentVariable.id,
        )
        .where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.agent_version_id == version_id,
            EnvironmentVariable.tenant_id == tenant_id,
            EnvironmentVariable.is_deleted == 0,
        )
        .order_by(EnvironmentVariable.name.asc(), EnvironmentVariable.id.asc())
    )
    return [
        EnvironmentVariableRefView(
            id=row.id,
            name=row.name,
            description=row.description,
            value="**",
        )
        for row in rows
    ]


async def _count_invalid_env_refs(session: AsyncSession, tenant_id: int, version_id: int) -> int:
    counted = await session.scalar(
        select(func.count())
        .select_from(AgentEnvironmentVariableRef)
        .outerjoin(
            EnvironmentVariable,
            and_(
                EnvironmentVariable.id == AgentEnvironmentVariableRef.environment_variable_id,
                EnvironmentVariable.tenant_id == tenant_id,
                EnvironmentVariable.is_deleted == 0,
            ),
        )
        .where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.agent_version_id == version_id,
            EnvironmentVariable.id.is_(None),
        )
    )
    return cast(int, counted)


async def _env_ref_exists(
    session: AsyncSession,
    tenant_id: int,
    version_id: int,
    environment_variable_id: int,
) -> bool:
    counted = await session.scalar(
        select(func.count())
        .select_from(AgentEnvironmentVariableRef)
        .where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.agent_version_id == version_id,
            AgentEnvironmentVariableRef.environment_variable_id == environment_variable_id,
        )
    )
    return cast(int, counted) > 0


async def _lock_variable(
    session: AsyncSession,
    tenant_id: int,
    environment_variable_id: int,
) -> EnvironmentVariable | None:
    return await session.scalar(
        select(EnvironmentVariable)
        .where(
            EnvironmentVariable.tenant_id == tenant_id,
            EnvironmentVariable.id == environment_variable_id,
            EnvironmentVariable.is_deleted == 0,
        )
        .limit(1)
        .with_for_update()
    )


async def _list_executors(session: AsyncSession, tenant_id: int, agent_id: int) -> list[Executor]:
    rows = await session.scalars(
        select(Executor)
        .where(
            Executor.tenant_id == tenant_id,
            Executor.agent_id == agent_id,
            Executor.is_deleted == 0,
        )
        .order_by(Executor.id.desc())
    )
    return list(rows)


def _duplicate_key(error: IntegrityError) -> bool:
    origin = error.orig
    if origin is None or not origin.args:
        return False
    return origin.args[0] == 1062
