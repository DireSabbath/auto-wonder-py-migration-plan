"""工单参与者和 @ 候选人。执行器在线仍看进程内会话。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank, java_is_whitespace
from autowonder.dispatch.models import Dispatch
from autowonder.evolution.jsontext import java_trim
from autowonder.executors.models import Executor
from autowonder.executors.registry import is_online
from autowonder.squads.models import SquadMember
from autowonder.users.models import User
from autowonder.workitems.models import Workitem, WorkitemComment, WorkitemCommentMention
from autowonder.workitems.schemas import ParticipantView
from autowonder.workitems.view import person_name
from autowonder.workspaces.models import OrgMember


async def get_participants(
    session: AsyncSession, workitem_id: int, tenant_id: int
) -> list[ParticipantView]:
    """真人来自创建人、负责人和评论；数字员工来自覆盖种子最多的小队。"""
    workitem = await _live(session, workitem_id)
    if workitem is None:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    dispatches = await _dispatches(session, tenant_id, workitem_id)
    seed_ids: list[int] = []
    for row in dispatches:
        if row.agent_id is not None and row.agent_id not in seed_ids:
            seed_ids.append(row.agent_id)
    if workitem.assignee_type == "AGENT" and workitem.assignee_ref is not None:
        if workitem.assignee_ref not in seed_ids:
            seed_ids.append(workitem.assignee_ref)
    members = await _resolve_squad_members(session, tenant_id, seed_ids)
    agent_ids = seed_ids
    if len(members) > 0:
        agent_ids = []
        for member in members:
            if member.agent_id is not None and member.agent_id not in agent_ids:
                agent_ids.append(member.agent_id)
    participants: list[ParticipantView] = []
    for human_id in await _human_ids(session, tenant_id, workitem):
        user = await _user(session, human_id)
        if user is not None:
            participants.append(human_participant(user))
    for agent_id in agent_ids:
        participants.append(await agent_participant(session, tenant_id, agent_id))
    return participants


async def get_mention_candidates(
    session: AsyncSession,
    workitem_id: int,
    tenant_id: int,
    query: str | None,
    limit: int,
) -> list[ParticipantView]:
    """参与者、已发布数字员工和空间成员，按类型和 id 去重。"""
    effective = 50
    if limit > 0:
        effective = min(limit, 100)
    chosen: dict[str, ParticipantView] = {}
    for participant in await get_participants(session, workitem_id, tenant_id):
        _add_candidate(chosen, participant, query, effective)
    agents = await session.scalars(
        select(Agent).where(Agent.tenant_id == tenant_id, Agent.is_deleted == 0)
    )
    for agent in agents.all():
        if agent.id is None or agent.online_version_id is None:
            continue
        participant = await agent_participant(session, tenant_id, agent.id)
        _add_candidate(chosen, participant, query, effective)
    members = await session.scalars(
        select(OrgMember).where(
            OrgMember.tenant_id == tenant_id,
            OrgMember.is_deleted == 0,
            OrgMember.status == 0,
        )
    )
    for member in members.all():
        if member.user_id is None:
            continue
        user = await _user(session, member.user_id)
        if user is not None:
            _add_candidate(chosen, human_participant(user), query, effective)
    return list(chosen.values())


def human_participant(user: User) -> ParticipantView:
    """真人参与者。在线状态固定为否。"""
    status = None
    if user.status is not None:
        status = str(user.status)
    return ParticipantView(
        user_id=user.id,
        target_type="HUMAN",
        name=person_name(user.nickname, user.username),
        display_id=str(user.id),
        agent=False,
        role="HUMAN",
        role_name="真人",
        online=False,
        status=status,
    )


async def agent_participant(
    session: AsyncSession, tenant_id: int, agent_id: int
) -> ParticipantView:
    """数字员工参与者。在线与否看执行器会话。"""
    view = ParticipantView(
        user_id=agent_id,
        target_type="AGENT",
        display_id=str(agent_id),
        agent=True,
        role="AGENT",
        role_name="开发小队成员",
        online=False,
    )
    agent = await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted == 0).limit(1)
    )
    if agent is not None:
        view.name = agent.name
        view.status = agent.status
    executor_status = await _executor_status(session, tenant_id, agent_id)
    view.executor_status = executor_status
    view.online = executor_status == "ONLINE" or executor_status == "BUSY"
    return view


def matches_mention_query(participant: ParticipantView, query: str | None) -> bool:
    """空查询保留全部。否则名称、展示 id 或角色名包含该词。"""
    if query is None or java_is_blank(query):
        return True
    normalized = java_trim(query).lower()
    if _contains(participant.name, normalized):
        return True
    if _contains(participant.display_id, normalized):
        return True
    return _contains(participant.role_name, normalized)


def plain_mention(content_md: str, name: str) -> bool:
    """``@名称`` 两侧都是分隔符才算明文提及。"""
    needle = "@" + name
    start = 0
    while start < len(content_md):
        index = content_md.find(needle, start)
        if index < 0:
            return False
        end = index + len(needle)
        left = index == 0 or _mention_boundary(content_md[index - 1])
        right = end == len(content_md) or _mention_boundary(content_md[end])
        if left and right:
            return True
        start = index + 1
    return False


async def _human_ids(session: AsyncSession, tenant_id: int, workitem: Workitem) -> list[int]:
    ids: list[int] = []
    if workitem.creator_id is not None:
        ids.append(workitem.creator_id)
    if workitem.assignee_type == "HUMAN" and workitem.assignee_ref is not None:
        if workitem.assignee_ref not in ids:
            ids.append(workitem.assignee_ref)
    comments = await session.scalars(
        select(WorkitemComment).where(
            WorkitemComment.tenant_id == workitem.tenant_id,
            WorkitemComment.source_type == "WORKITEM",
            WorkitemComment.workitem_id == workitem.id,
        )
    )
    for comment in comments.all():
        if comment.author_type == "HUMAN" and comment.author_ref not in ids:
            ids.append(comment.author_ref)
    mentions = await session.scalars(
        select(WorkitemCommentMention).where(
            WorkitemCommentMention.tenant_id == tenant_id,
            WorkitemCommentMention.source_type == "WORKITEM",
            WorkitemCommentMention.workitem_id == workitem.id,
        )
    )
    for mention in mentions.all():
        if mention.target_type == "HUMAN" and mention.target_ref not in ids:
            ids.append(mention.target_ref)
    return ids


async def _resolve_squad_members(
    session: AsyncSession, tenant_id: int, agent_ids: list[int]
) -> list[SquadMember]:
    if len(agent_ids) == 0:
        return []
    seeds = set(agent_ids)
    best: list[SquadMember] = []
    best_score = 0
    visited: set[int] = set()
    for agent_id in agent_ids:
        links = await session.scalars(select(SquadMember).where(SquadMember.agent_id == agent_id))
        for link in links.all():
            if link.squad_id is None or link.squad_id in visited:
                continue
            visited.add(link.squad_id)
            members = await session.scalars(
                select(SquadMember).where(SquadMember.squad_id == link.squad_id)
            )
            same_tenant = [member for member in members.all() if member.tenant_id == tenant_id]
            score = 0
            for member in same_tenant:
                if member.agent_id in seeds:
                    score += 1
            if score > best_score or (score == best_score and len(same_tenant) > len(best)):
                best_score = score
                best = same_tenant
    return best


async def _executor_status(session: AsyncSession, tenant_id: int, agent_id: int) -> str:
    rows = await session.scalars(
        select(Executor).where(
            Executor.tenant_id == tenant_id,
            Executor.agent_id == agent_id,
            Executor.is_deleted == 0,
        )
    )
    online = [row for row in rows.all() if row.id is not None and is_online(row.id)]
    for row in online:
        if row.status == "BUSY":
            return "BUSY"
    if len(online) > 0:
        return "ONLINE"
    return "OFFLINE"


def _add_candidate(
    chosen: dict[str, ParticipantView],
    participant: ParticipantView,
    query: str | None,
    limit: int,
) -> None:
    if participant.user_id is None or participant.name is None or java_is_blank(participant.name):
        return
    if len(chosen) >= limit:
        return
    if not matches_mention_query(participant, query):
        return
    target_type = participant.target_type
    if target_type is None:
        target_type = "HUMAN"
        if participant.agent:
            target_type = "AGENT"
    chosen.setdefault(target_type + ":" + str(participant.user_id), participant)


def _contains(value: str | None, normalized: str) -> bool:
    if value is None:
        return False
    return normalized in value.lower()


def _mention_boundary(char: str) -> bool:
    if java_is_whitespace(ord(char)):
        return True
    return char in ",.;:!?)]}\uFF0C\u3002\uFF1B\uFF1A\uFF01\uFF1F\uFF09\u3011\u300D"


async def _live(session: AsyncSession, workitem_id: int) -> Workitem | None:
    return await session.scalar(
        select(Workitem).where(Workitem.id == workitem_id, Workitem.is_deleted == 0).limit(1)
    )


async def _user(session: AsyncSession, user_id: int) -> User | None:
    return await session.scalar(
        select(User).where(User.id == user_id, User.is_deleted == 0).limit(1)
    )


async def _dispatches(
    session: AsyncSession, tenant_id: int, workitem_id: int
) -> list[Dispatch]:
    result = await session.scalars(
        select(Dispatch).where(
            Dispatch.tenant_id == tenant_id,
            Dispatch.source_type == "WORKITEM",
            Dispatch.workitem_id == workitem_id,
            Dispatch.is_deleted == 0,
        )
    )
    return list(result.all())
