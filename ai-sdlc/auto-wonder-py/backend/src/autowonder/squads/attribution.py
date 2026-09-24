"""从成员关系反查小队。数字员工、流程和执行器卡片共用这组引用。"""

from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentVersion
from autowonder.squads.service import list_by_ids, list_members_by_agent_ids


@dataclass(frozen=True)
class SquadRefs:
    """一组小队 id 与同序名称。已删除小队不出现。"""

    ids: list[int]
    names: list[str]


_EMPTY = SquadRefs(ids=[], names=[])


async def refs_by_agent_ids(
    session: AsyncSession,
    tenant_id: int | None,
    agent_ids: list[int | None],
) -> dict[int, SquadRefs]:
    """按数字员工汇总所在小队。"""
    distinct_ids = _distinct(agent_ids)
    if tenant_id is None or len(distinct_ids) == 0:
        return {}
    squad_ids_by_key: dict[int, dict[int, None]] = {}
    all_squad_ids: dict[int, None] = {}
    for member in await list_members_by_agent_ids(session, tenant_id, distinct_ids):
        bucket = squad_ids_by_key.setdefault(member.agent_id, {})
        bucket[member.squad_id] = None
        all_squad_ids[member.squad_id] = None
    return await _to_refs(session, squad_ids_by_key, list(all_squad_ids))


async def refs_by_sdlc_ids(
    session: AsyncSession,
    tenant_id: int | None,
    sdlc_ids: list[int | None],
) -> dict[int, SquadRefs]:
    """按在线版本上的流程汇总所在小队。"""
    distinct_ids = _distinct(sdlc_ids)
    if tenant_id is None or len(distinct_ids) == 0:
        return {}
    agent_ids_by_sdlc: dict[int, dict[int, None]] = {}
    agent_ids: dict[int, None] = {}
    for sdlc_id, agent_id in await _online_agents_by_sdlc(session, tenant_id, distinct_ids):
        agent_ids_by_sdlc.setdefault(sdlc_id, {})[agent_id] = None
        agent_ids[agent_id] = None
    if len(agent_ids) == 0:
        return {}
    squad_ids_by_agent: dict[int, dict[int, None]] = {}
    all_squad_ids: dict[int, None] = {}
    for member in await list_members_by_agent_ids(session, tenant_id, list(agent_ids)):
        squad_ids_by_agent.setdefault(member.agent_id, {})[member.squad_id] = None
        all_squad_ids[member.squad_id] = None
    squad_ids_by_sdlc: dict[int, dict[int, None]] = {}
    for sdlc_id, linked_agents in agent_ids_by_sdlc.items():
        merged = squad_ids_by_sdlc.setdefault(sdlc_id, {})
        for agent_id in linked_agents:
            for squad_id in squad_ids_by_agent.get(agent_id, {}):
                merged[squad_id] = None
    return await _to_refs(session, squad_ids_by_sdlc, list(all_squad_ids))


def assemble_refs(
    squad_ids_by_key: dict[int, dict[int, None]],
    name_by_id: dict[int, str],
) -> dict[int, SquadRefs]:
    """丢掉没有名称的小队，保留成员关系里的先后顺序。"""
    result: dict[int, SquadRefs] = {}
    for key, squad_ids in squad_ids_by_key.items():
        ids: list[int] = []
        names: list[str] = []
        for squad_id in squad_ids:
            name = name_by_id.get(squad_id)
            if name is None:
                continue
            ids.append(squad_id)
            names.append(name)
        result[key] = SquadRefs(ids=ids, names=names)
    return result


def empty_refs() -> SquadRefs:
    """没有关联小队时的空引用。"""
    return _EMPTY


async def _to_refs(
    session: AsyncSession,
    squad_ids_by_key: dict[int, dict[int, None]],
    all_squad_ids: list[int],
) -> dict[int, SquadRefs]:
    if len(squad_ids_by_key) == 0:
        return {}
    name_by_id = {squad.id: squad.name for squad in await list_by_ids(session, all_squad_ids)}
    return assemble_refs(squad_ids_by_key, name_by_id)


async def _online_agents_by_sdlc(
    session: AsyncSession,
    tenant_id: int,
    sdlc_ids: list[int],
) -> list[tuple[int, int]]:
    rows = await session.execute(
        select(AgentVersion.sdlc_id, AgentVersion.agent_id)
        .join(
            Agent,
            and_(
                Agent.online_version_id == AgentVersion.id,
                Agent.tenant_id == tenant_id,
                Agent.is_deleted == 0,
            ),
        )
        .where(
            AgentVersion.is_deleted == 0,
            AgentVersion.tenant_id == tenant_id,
            AgentVersion.sdlc_id.in_(sdlc_ids),
        )
        .distinct()
        .order_by(AgentVersion.sdlc_id, AgentVersion.agent_id)
    )
    pairs: list[tuple[int, int]] = []
    for sdlc_id, agent_id in rows.all():
        if sdlc_id is None or agent_id is None:
            continue
        pairs.append((sdlc_id, agent_id))
    return pairs


def _distinct(values: list[int | None]) -> list[int]:
    ordered: dict[int, None] = {}
    for value in values:
        if value is not None:
            ordered[value] = None
    return list(ordered)
