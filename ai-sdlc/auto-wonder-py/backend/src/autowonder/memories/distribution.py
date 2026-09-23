"""已采纳记忆分发给在线数字员工。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.agents.service import attach_reviewed_memory
from autowonder.squads.models import SquadMember


async def distribute(
    session: AsyncSession,
    *,
    memory_id: int,
    tenant_id: int,
    status: str,
    scope: str | None,
    owner_ref: int | None,
    user_id: int,
) -> None:
    """只把已采纳记忆挂到仍有在线版本、且属于同一工作空间的员工。"""
    if status != "ADOPTED":
        return
    for agent in await _targets(session, tenant_id, scope, owner_ref):
        if agent.tenant_id != tenant_id:
            continue
        if agent.online_version_id is None:
            continue
        await attach_reviewed_memory(
            session,
            agent.id,
            memory_id,
            scope,
            tenant_id,
            user_id,
        )


async def _targets(
    session: AsyncSession,
    tenant_id: int,
    scope: str | None,
    owner_ref: int | None,
) -> list[Agent]:
    if scope == "AGENT" and owner_ref is not None:
        agent = await session.scalar(
            select(Agent).where(Agent.id == owner_ref, Agent.is_deleted == 0).limit(1)
        )
        if agent is None:
            return []
        return [agent]
    if scope == "SQUAD" and owner_ref is not None:
        return await _squad_agents(session, tenant_id, owner_ref)
    if scope == "ORG":
        rows = await session.scalars(
            select(Agent)
            .where(Agent.tenant_id == tenant_id, Agent.is_deleted == 0)
            .order_by(Agent.id.asc())
        )
        return list(rows)
    return []


async def _squad_agents(
    session: AsyncSession,
    tenant_id: int,
    squad_id: int,
) -> list[Agent]:
    members = list(
        await session.scalars(select(SquadMember).where(SquadMember.squad_id == squad_id))
    )
    ids: list[int] = []
    seen: set[int] = set()
    for member in members:
        if member.tenant_id != tenant_id:
            continue
        if member.agent_id in seen:
            continue
        seen.add(member.agent_id)
        ids.append(member.agent_id)
    if len(ids) == 0:
        return []
    rows = await session.scalars(
        select(Agent).where(
            Agent.tenant_id == tenant_id,
            Agent.is_deleted == 0,
            Agent.id.in_(ids),
        )
    )
    return list(rows)
