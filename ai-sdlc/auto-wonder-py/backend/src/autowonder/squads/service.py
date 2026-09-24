"""小队的创建、列表、成员和卡片统计。查询口径对齐 SquadDao / SquadService。"""

from typing import Any, cast

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentVersion
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.executors.models import Executor
from autowonder.executors.registry import is_online, presence
from autowonder.sdlcs.models import Sdlc, SdlcStep
from autowonder.squads.models import Squad, SquadMember
from autowonder.squads.schemas import (
    AddMembersRequest,
    CreateSquadRequest,
    ExecutorSummary,
    SdlcStepSummary,
    SdlcSummary,
    SquadMemberView,
    SquadView,
    UpdateSquadRequest,
)

_MEMBER_BATCH_LIMIT = 50


def require_squad_name(name: str | None) -> str:
    """创建时名称必填，并去掉两端空白。"""
    if name is None or name.strip() == "":
        raise BizError(ErrorCode.SQUAD_NAME_REQUIRED)
    return name.strip()


def reject_blank_squad_name(name: str | None) -> None:
    """更新时只拒绝显式传入的空白名称。"""
    if name is not None and name.strip() == "":
        raise BizError(ErrorCode.SQUAD_NAME_REQUIRED)


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


async def create_squad(
    session: AsyncSession,
    request: CreateSquadRequest,
    tenant_id: int,
    user_id: int,
) -> SquadView:
    """插入小队。插入语句不回读创建时间，响应该字段保持 null。"""
    squad = Squad(
        tenant_id=tenant_id,
        name=require_squad_name(request.name),
        description=request.description,
        owner_id=request.owner_id,
        status=0,
        creator_id=user_id,
        is_deleted=0,
        version=0,
    )
    session.add(squad)
    await session.flush()
    await session.commit()
    view = _to_view(squad, [], 0)
    view.gmt_create = None
    return view


async def get_squad(session: AsyncSession, squad_id: int) -> SquadView:
    """小队详情，带成员数字员工、流程和执行器。"""
    squad = await _require_squad(session, squad_id)
    members = await _members(session, squad_id)
    agent_ids = [member.agent_id for member in members]
    view = _to_view(squad, agent_ids, len(agent_ids))
    await _fill_sdlcs_and_executors(session, view, squad.tenant_id, agent_ids)
    return view


async def list_squads(session: AsyncSession, page: int, size: int) -> list[SquadView]:
    """在用小队，按 id 倒序。"""
    offset, limit = page_window(page, size)
    rows = await session.scalars(
        select(Squad)
        .where(Squad.is_deleted == 0, Squad.status == 0)
        .order_by(Squad.id.desc())
        .offset(offset)
        .limit(limit)
    )
    views: list[SquadView] = []
    for squad in rows:
        views.append(await _to_list_view(session, squad))
    return views


async def update_squad(
    session: AsyncSession,
    squad_id: int,
    request: UpdateSquadRequest,
    tenant_id: int,
    user_id: int,
) -> SquadView:
    """按 version 更新。未传 debug 开关时保留原值。"""
    squad = await _require_squad(session, squad_id)
    reject_blank_squad_name(request.name)
    values: dict[str, Any] = {
        "name": request.name,
        "description": request.description,
        "owner_id": request.owner_id,
        "version": Squad.version + 1,
        "modifier_id": user_id,
    }
    if request.debug_log_enabled is not None:
        flag = 0
        if request.debug_log_enabled:
            flag = 1
        values["debug_log_enabled"] = flag
    updated = rowcount(
        await session.execute(
            update(Squad)
            .where(
                Squad.id == squad_id,
                Squad.tenant_id == tenant_id,
                Squad.version == squad.version,
                Squad.is_deleted == 0,
            )
            .values(**values)
        )
    )
    if updated == 0:
        raise BizError(ErrorCode.CONFLICT)
    session.expire_all()
    view = await get_squad(session, squad_id)
    await session.commit()
    return view


async def delete_squad(
    session: AsyncSession,
    squad_id: int,
    tenant_id: int,
    user_id: int,
) -> None:
    """逻辑删除小队，并物理删除其成员。"""
    squad = await _require_squad(session, squad_id)
    deleted = rowcount(
        await session.execute(
            update(Squad)
            .where(
                Squad.id == squad_id,
                Squad.tenant_id == tenant_id,
                Squad.version == squad.version,
                Squad.is_deleted == 0,
            )
            .values(
                is_deleted=1,
                status=1,
                version=Squad.version + 1,
                modifier_id=user_id,
            )
        )
    )
    if deleted == 0:
        raise BizError(ErrorCode.CONFLICT)
    await session.execute(
        delete(SquadMember).where(
            SquadMember.squad_id == squad_id,
            SquadMember.tenant_id == tenant_id,
        )
    )
    await session.commit()


async def add_members(
    session: AsyncSession,
    squad_id: int,
    request: AddMembersRequest,
    tenant_id: int,
) -> None:
    """追加成员。已在小队中的数字员工跳过，一批最多 50 个。"""
    agent_ids = request.agent_ids
    if agent_ids is None or len(agent_ids) == 0:
        return
    if len(agent_ids) > _MEMBER_BATCH_LIMIT:
        raise BizError(ErrorCode.PARAM_INVALID)
    await _require_squad(session, squad_id)
    for agent_id in agent_ids:
        existing = await session.scalar(
            select(SquadMember.id)
            .where(SquadMember.squad_id == squad_id, SquadMember.agent_id == agent_id)
            .limit(1)
        )
        if existing is not None:
            continue
        session.add(SquadMember(tenant_id=tenant_id, squad_id=squad_id, agent_id=agent_id))
    await session.commit()


async def remove_member(
    session: AsyncSession,
    squad_id: int,
    agent_id: int,
    tenant_id: int,
) -> None:
    """移除成员。没有对应行时也成功。"""
    await _require_squad(session, squad_id)
    await session.execute(
        delete(SquadMember).where(
            SquadMember.squad_id == squad_id,
            SquadMember.agent_id == agent_id,
            SquadMember.tenant_id == tenant_id,
        )
    )
    await session.commit()


async def list_members(
    session: AsyncSession,
    squad_id: int,
    tenant_id: int,
) -> list[SquadMemberView]:
    """成员及其在线版本上的角色和流程步骤。"""
    await _require_squad(session, squad_id)
    members = await _members(session, squad_id)
    if len(members) == 0:
        return []
    agent_ids = [member.agent_id for member in members]
    agents = await _agents_by_ids(session, tenant_id, agent_ids)
    version_ids = {
        agent.online_version_id for agent in agents.values() if agent.online_version_id is not None
    }
    versions = await _versions_by_ids(session, tenant_id, version_ids)
    sdlc_ids = {version.sdlc_id for version in versions.values() if version.sdlc_id is not None}
    sdlcs: dict[int, Sdlc] = {}
    steps_by_sdlc: dict[int, list[SdlcStepSummary]] = {}
    for sdlc_id in sdlc_ids:
        sdlc = await _find_sdlc(session, sdlc_id)
        if sdlc is not None:
            sdlcs[sdlc_id] = sdlc
        steps_by_sdlc[sdlc_id] = _step_summaries(await _steps(session, sdlc_id))
    views: list[SquadMemberView] = []
    for member in members:
        view = SquadMemberView(agent_id=member.agent_id)
        agent = agents.get(member.agent_id)
        if agent is not None:
            view.agent_name = agent.name
            view.agent_kind = agent.kind
            _fill_member_version(view, agent, versions, sdlcs, steps_by_sdlc)
        views.append(view)
    return views


async def list_squads_by_agent(session: AsyncSession, agent_id: int) -> list[int]:
    """数字员工所在的小队 id。"""
    rows = await session.scalars(
        select(SquadMember.squad_id).where(SquadMember.agent_id == agent_id)
    )
    return list(rows)


async def count_debug_enabled_by_agent(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
) -> int:
    """该数字员工所在、且打开了 debug 日志的未删除小队数量。"""
    counted = await session.scalar(
        select(func.count())
        .select_from(SquadMember)
        .join(Squad, Squad.id == SquadMember.squad_id)
        .where(
            SquadMember.agent_id == agent_id,
            SquadMember.tenant_id == tenant_id,
            Squad.tenant_id == tenant_id,
            Squad.is_deleted == 0,
            Squad.debug_log_enabled == 1,
        )
    )
    return cast(int, counted)


async def list_by_ids(session: AsyncSession, squad_ids: list[int]) -> list[Squad]:
    """按 id 升序读取未删除小队。空集合不生成 IN ()。"""
    if len(squad_ids) == 0:
        return []
    rows = await session.scalars(
        select(Squad)
        .where(Squad.is_deleted == 0, Squad.id.in_(squad_ids))
        .order_by(Squad.id.asc())
    )
    return list(rows)


async def list_members_by_agent_ids(
    session: AsyncSession,
    tenant_id: int,
    agent_ids: list[int],
) -> list[SquadMember]:
    """按数字员工批量读取成员关系，顺序为 agent_id、squad_id。"""
    if len(agent_ids) == 0:
        return []
    rows = await session.scalars(
        select(SquadMember)
        .where(SquadMember.tenant_id == tenant_id, SquadMember.agent_id.in_(agent_ids))
        .order_by(SquadMember.agent_id, SquadMember.squad_id)
    )
    return list(rows)


def _to_view(
    squad: Squad,
    member_agent_ids: list[int] | None,
    member_count: int,
) -> SquadView:
    return SquadView(
        id=squad.id,
        name=squad.name,
        description=squad.description,
        owner_id=squad.owner_id,
        version=squad.version,
        debug_log_enabled=squad.debug_log_enabled == 1,
        gmt_create=squad.gmt_create,
        member_agent_ids=member_agent_ids,
        member_count=member_count,
    )


async def _to_list_view(session: AsyncSession, squad: Squad) -> SquadView:
    members = await _members(session, squad.id)
    view = _to_view(squad, None, len(members))
    if len(members) == 0:
        return view
    agent_ids = list(dict.fromkeys(member.agent_id for member in members))
    agents = await _agents_by_ids(session, squad.tenant_id, agent_ids)
    version_ids = {
        agent.online_version_id for agent in agents.values() if agent.online_version_id is not None
    }
    versions = await _versions_by_ids(session, squad.tenant_id, version_ids)
    roles: set[str] = set()
    sdlc_ids: set[int] = set()
    for version in versions.values():
        role = version.role_code
        if role is None:
            role = version.role_name
        if role is not None:
            roles.add(role)
        if version.sdlc_id is not None:
            sdlc_ids.add(version.sdlc_id)
    executor_total = 0
    executor_online = 0
    for agent_id in agent_ids:
        executors = await _executors_by_agent(session, squad.tenant_id, agent_id)
        executor_total += len(executors)
        for executor, _agent_name in executors:
            if is_online(executor.id):
                executor_online += 1
    view.role_count = len(roles)
    view.sdlc_count = len(sdlc_ids)
    view.executor_total_count = executor_total
    view.executor_online_count = executor_online
    return view


async def _fill_sdlcs_and_executors(
    session: AsyncSession,
    view: SquadView,
    tenant_id: int,
    member_agent_ids: list[int],
) -> None:
    agent_ids = list(dict.fromkeys(member_agent_ids))
    if len(agent_ids) == 0:
        view.sdlcs = []
        view.executors = []
        return
    executors = await _executors_by_agents(session, tenant_id, agent_ids)
    executors.sort(key=lambda row: row[0].id)
    view.executors = [
        ExecutorSummary(
            id=executor.id,
            agent_id=executor.agent_id,
            agent_name=agent_name,
            name=executor.name,
            status=presence(executor.id),
            client_kind=executor.client_kind,
            last_heartbeat=executor.last_heartbeat,
        )
        for executor, agent_name in executors
    ]
    sdlc_ids = await _online_sdlc_ids(session, tenant_id, agent_ids)
    sdlcs: list[Sdlc] = []
    if len(sdlc_ids) > 0:
        rows = await session.scalars(
            select(Sdlc).where(Sdlc.is_deleted == 0, Sdlc.id.in_(sdlc_ids))
        )
        sdlcs = list(rows)
    sdlcs.sort(key=lambda row: row.id)
    view.sdlcs = [
        SdlcSummary(id=sdlc.id, name=sdlc.name, work_type=sdlc.work_type, status=sdlc.status)
        for sdlc in sdlcs
    ]


async def _online_sdlc_ids(
    session: AsyncSession,
    tenant_id: int,
    agent_ids: list[int],
) -> list[int]:
    agents = await _agents_by_ids(session, tenant_id, agent_ids)
    version_ids = {
        agent.online_version_id for agent in agents.values() if agent.online_version_id is not None
    }
    versions = await _versions_by_ids(session, tenant_id, version_ids)
    sdlc_ids: dict[int, None] = {}
    for version in versions.values():
        if version.sdlc_id is not None:
            sdlc_ids[version.sdlc_id] = None
    return list(sdlc_ids)


def _fill_member_version(
    view: SquadMemberView,
    agent: Agent,
    versions: dict[int, AgentVersion],
    sdlcs: dict[int, Sdlc],
    steps_by_sdlc: dict[int, list[SdlcStepSummary]],
) -> None:
    if agent.online_version_id is None:
        return
    version = versions.get(agent.online_version_id)
    if version is None:
        return
    view.role_code = version.role_code
    view.role_name = version.role_name
    view.responsibilities = version.responsibilities
    view.sdlc_id = version.sdlc_id
    if version.sdlc_id is None:
        return
    sdlc = sdlcs.get(version.sdlc_id)
    if sdlc is not None:
        view.sdlc_name = sdlc.name
    steps = steps_by_sdlc.get(version.sdlc_id)
    if steps is None:
        view.sdlc_steps = []
        return
    view.sdlc_steps = steps


def _step_summaries(steps: list[SdlcStep]) -> list[SdlcStepSummary]:
    ordered = sorted(steps, key=lambda step: step.step_order)
    return [
        SdlcStepSummary(
            id=step.id,
            step_order=step.step_order,
            name=step.name,
            handler_type=step.handler_type,
            handler_role_ref=step.handler_role_ref,
        )
        for step in ordered
    ]


async def _require_squad(session: AsyncSession, squad_id: int) -> Squad:
    squad = await session.scalar(
        select(Squad).where(Squad.id == squad_id, Squad.is_deleted == 0).limit(1)
    )
    if squad is None:
        raise BizError(ErrorCode.SQUAD_NOT_FOUND)
    return squad


async def _members(session: AsyncSession, squad_id: int) -> list[SquadMember]:
    rows = await session.scalars(select(SquadMember).where(SquadMember.squad_id == squad_id))
    return list(rows)


async def _agents_by_ids(
    session: AsyncSession,
    tenant_id: int,
    agent_ids: list[int],
) -> dict[int, Agent]:
    if len(agent_ids) == 0:
        return {}
    rows = await session.scalars(
        select(Agent).where(
            Agent.tenant_id == tenant_id,
            Agent.is_deleted == 0,
            Agent.id.in_(agent_ids),
        )
    )
    return {agent.id: agent for agent in rows}


async def _versions_by_ids(
    session: AsyncSession,
    tenant_id: int,
    version_ids: set[int],
) -> dict[int, AgentVersion]:
    if len(version_ids) == 0:
        return {}
    rows = await session.scalars(
        select(AgentVersion).where(
            AgentVersion.tenant_id == tenant_id,
            AgentVersion.is_deleted == 0,
            AgentVersion.id.in_(version_ids),
        )
    )
    return {version.id: version for version in rows}


async def _find_sdlc(session: AsyncSession, sdlc_id: int) -> Sdlc | None:
    return await session.scalar(
        select(Sdlc).where(Sdlc.id == sdlc_id, Sdlc.is_deleted == 0).limit(1)
    )


async def _steps(session: AsyncSession, sdlc_id: int) -> list[SdlcStep]:
    rows = await session.scalars(
        select(SdlcStep)
        .where(SdlcStep.sdlc_id == sdlc_id, SdlcStep.is_deleted == 0)
        .order_by(SdlcStep.step_order.asc())
    )
    return list(rows)


async def _executors_by_agent(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
) -> list[tuple[Executor, str | None]]:
    result = await session.execute(_executor_query(tenant_id).where(Executor.agent_id == agent_id))
    return [(executor, agent_name) for executor, agent_name in result.all()]


async def _executors_by_agents(
    session: AsyncSession,
    tenant_id: int,
    agent_ids: list[int],
) -> list[tuple[Executor, str | None]]:
    result = await session.execute(
        _executor_query(tenant_id).where(Executor.agent_id.in_(agent_ids))
    )
    return [(executor, agent_name) for executor, agent_name in result.all()]


def _executor_query(tenant_id: int) -> Any:
    return (
        select(Executor, Agent.name)
        .outerjoin(
            Agent,
            and_(
                Agent.id == Executor.agent_id,
                Agent.tenant_id == Executor.tenant_id,
                Agent.is_deleted == 0,
            ),
        )
        .where(Executor.tenant_id == tenant_id, Executor.is_deleted == 0)
        .order_by(Executor.id.desc())
    )
