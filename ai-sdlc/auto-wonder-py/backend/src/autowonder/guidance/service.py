"""评论 @ 数字员工后写入投递状态，并插入对应的 PENDING 调度。"""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.core.clock import now_local
from autowonder.core.errors import IllegalArgumentError
from autowonder.db.rows import rowcount
from autowonder.debuglogs.sanitizer import java_is_blank, java_strip
from autowonder.dispatch.enqueue import (
    ACTIVE_TURN_STATUSES,
    enqueue_comment_interaction,
    enqueue_workitem,
    has_resumable_session,
    list_workitem_dispatches,
)
from autowonder.dispatch.models import Dispatch
from autowonder.guidance.mentions import (
    html_text,
    mention_comparable_content,
    mention_names,
    text_mention_index,
)
from autowonder.notifications.models import WorkitemCommentDelivery
from autowonder.sdlcs.models import SdlcStep
from autowonder.workitems.comments import add_agent_comment
from autowonder.workitems.models import Workitem, WorkitemComment
from autowonder.workitems.participants import get_participants
from autowonder.workitems.schemas import CommentInteractionView, TimelineItemView
from autowonder.workitems.service import _find_agent, _version_sdlc, rebind_for_interaction_rework

logger = logging.getLogger(__name__)

FORMAL_ACK = "收到，已转入正式工作流程。"
_FAILED_TURN = frozenset({"FAILED", "TIMEOUT", "CANCELED"})


async def create_for_comment(
    session: AsyncSession,
    workspace_id: int,
    workitem_id: int,
    comment_id: int,
    content_md: str | None,
    explicit_target_agent_ids: list[int | None] | None,
    creator_id: int,
) -> None:
    """显式数字员工优先。否则从正文解析唯一的 @。"""
    targets: list[int] = []
    if explicit_target_agent_ids is not None:
        for agent_id in explicit_target_agent_ids:
            if agent_id is not None and agent_id not in targets:
                targets.append(agent_id)
    if len(targets) == 0:
        targets = await _resolve_mention(session, workspace_id, workitem_id, content_md)
    for agent_id in targets:
        await _create(
            session, workspace_id, workitem_id, comment_id, agent_id, creator_id, content_md
        )


async def attach_interaction_statuses(
    session: AsyncSession,
    workspace_id: int,
    workitem_id: int,
    timeline: list[TimelineItemView],
) -> None:
    """把投递状态挂到评论上，并拿掉已经嵌进回复里的评论。"""
    comments: dict[int, TimelineItemView] = {}
    for item in timeline:
        if item is not None and item.type == "comment" and item.id is not None:
            comments[item.id] = item
    result = await session.scalars(
        select(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.tenant_id == workspace_id,
            WorkitemCommentDelivery.source_type == "WORKITEM",
            WorkitemCommentDelivery.workitem_id == workitem_id,
        )
        .order_by(WorkitemCommentDelivery.id.asc())
    )
    nested: set[int] = set()
    for guidance in result.all():
        if guidance.status == "APPLIED" and guidance.reply_comment_id is None:
            continue
        comment = comments.get(guidance.comment_id)
        if comment is None:
            continue
        interaction = await _interaction(session, workspace_id, guidance, comments, nested)
        if comment.interactions is None:
            comment.interactions = []
        comment.interactions.append(interaction)
    if len(nested) == 0:
        return
    timeline[:] = [
        item
        for item in timeline
        if not (item is not None and item.type == "comment" and item.id in nested)
    ]


async def _create(
    session: AsyncSession,
    workspace_id: int,
    workitem_id: int,
    comment_id: int,
    target_agent_id: int,
    creator_id: int,
    content_md: str | None,
) -> WorkitemCommentDelivery:
    workitem = await session.scalar(
        select(Workitem).where(Workitem.id == workitem_id, Workitem.is_deleted == 0).limit(1)
    )
    if workitem is None or workitem.tenant_id != workspace_id:
        raise IllegalArgumentError("workitem does not belong to tenant")
    agent = await session.scalar(
        select(Agent).where(Agent.id == target_agent_id, Agent.is_deleted == 0).limit(1)
    )
    if agent is None or agent.tenant_id != workspace_id:
        raise IllegalArgumentError("target agent does not belong to tenant")
    await _require_comment(session, workspace_id, workitem_id, comment_id)
    guidance = WorkitemCommentDelivery(
        tenant_id=workspace_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        comment_id=comment_id,
        target_agent_id=target_agent_id,
    )
    if agent.online_version_id is None:
        guidance.status = "FAILED"
        guidance.error = "目标数字员工未发布在线版本，无法启动会话"
        session.add(guidance)
        await session.flush()
        return guidance
    guidance.status = "QUEUED"
    session.add(guidance)
    await session.flush()
    dispatches = await list_workitem_dispatches(session, workspace_id, workitem_id)
    prior = _latest_for_agent(dispatches, target_agent_id)
    sdlc_id = await _sdlc_id(session, workspace_id, target_agent_id)
    first_step = await _first_step(session, workspace_id, sdlc_id)
    mention_only = _mention_only(content_md, agent.name)
    no_history = prior is None and not _has_history(dispatches, target_agent_id)
    if no_history and mention_only and sdlc_id is not None and first_step is not None:
        await rebind_for_interaction_rework(
            session, workspace_id, workitem_id, target_agent_id, sdlc_id, first_step.id, creator_id
        )
        acknowledgement, _notices = await add_agent_comment(
            session, workitem_id, FORMAL_ACK, [], workspace_id, target_agent_id, None
        )
        if acknowledgement.id is None:
            raise RuntimeError("failed to record formal workflow acknowledgement")
        bound = await _bind_reply(session, guidance.id, workspace_id, acknowledgement.id)
        if bound != 1:
            raise RuntimeError("failed to record formal workflow acknowledgement")
        formal = await enqueue_workitem(
            session, workspace_id, workitem_id, first_step.id, target_agent_id, 1, creator_id
        )
        guidance.dispatch_id = formal.id
        if await _bind_dispatch(session, guidance.id, workspace_id, formal.id) != 1:
            raise RuntimeError("failed to bind formal worker dispatch")
        await _set_status(session, guidance.id, workspace_id, "APPLIED", None)
        guidance.status = "APPLIED"
        logger.info(
            "guidance dispatch queued workspaceId=%s dispatchId=%s", workspace_id, formal.id
        )
        return guidance
    fork = False
    if prior is not None and prior.status in ACTIVE_TURN_STATUSES:
        fork = await has_resumable_session(session, workspace_id, prior.id)
    step_id = None
    if first_step is not None:
        step_id = first_step.id
    source_id = None
    if prior is not None:
        source_id = prior.id
    interaction = await enqueue_comment_interaction(
        session,
        workspace_id,
        workitem_id,
        target_agent_id,
        source_id,
        fork,
        step_id,
        guidance.id,
        creator_id,
    )
    guidance.dispatch_id = interaction.id
    if await _bind_dispatch(session, guidance.id, workspace_id, interaction.id) != 1:
        raise RuntimeError("failed to bind comment interaction dispatch")
    logger.info(
        "guidance dispatch queued workspaceId=%s dispatchId=%s", workspace_id, interaction.id
    )
    return guidance


async def _resolve_mention(
    session: AsyncSession, workspace_id: int, workitem_id: int, content_md: str | None
) -> list[int]:
    if content_md is None:
        return []
    for name in mention_names(content_md):
        matched: list[int] = []
        for participant in await get_participants(session, workitem_id, workspace_id):
            if participant is None or not participant.agent:
                continue
            if participant.user_id is None or participant.name is None:
                continue
            if java_strip(participant.name) == name and participant.user_id not in matched:
                matched.append(participant.user_id)
        if len(matched) == 1:
            return matched
        named = await session.scalars(
            select(Agent).where(
                Agent.tenant_id == workspace_id,
                Agent.name == name,
                Agent.is_deleted == 0,
            )
        )
        tenant_matches = [
            agent.id
            for agent in named.all()
            if agent.tenant_id == workspace_id and agent.id is not None
        ]
        if len(tenant_matches) == 1:
            return tenant_matches
    return await _plain_agent(session, workspace_id, html_text(content_md))


async def _plain_agent(session: AsyncSession, workspace_id: int, content: str) -> list[int]:
    if "@" not in content:
        return []
    earliest = 2**31 - 1
    longest = -1
    matched: list[int] = []
    agents = await session.scalars(
        select(Agent).where(Agent.tenant_id == workspace_id, Agent.is_deleted == 0)
    )
    for agent in agents.all():
        if agent.tenant_id != workspace_id or agent.id is None or agent.name is None:
            continue
        if java_is_blank(agent.name):
            continue
        name = java_strip(agent.name)
        index = text_mention_index(content, name)
        if index < 0:
            continue
        if index < earliest or (index == earliest and len(name) > longest):
            earliest = index
            longest = len(name)
            matched = []
        if index == earliest and len(name) == longest:
            matched.append(agent.id)
    distinct: list[int] = []
    for agent_id in matched:
        if agent_id not in distinct:
            distinct.append(agent_id)
    if len(distinct) == 1:
        return distinct
    return []


def _mention_only(content_md: str | None, agent_name: str | None) -> bool:
    if content_md is None or agent_name is None or java_is_blank(agent_name):
        return False
    return java_strip(mention_comparable_content(content_md)) == "@" + java_strip(agent_name)


def _has_history(dispatches: list[Dispatch], agent_id: int) -> bool:
    for row in dispatches:
        if row is not None and row.agent_id == agent_id:
            return True
    return False


def _latest_for_agent(dispatches: list[Dispatch], agent_id: int) -> Dispatch | None:
    latest: Dispatch | None = None
    for row in dispatches:
        if row.agent_id != agent_id or row.resume_mode == "SIDE_INTERACTION":
            continue
        if latest is None or row.id > latest.id:
            latest = row
    return latest


async def _sdlc_id(session: AsyncSession, workspace_id: int, agent_id: int) -> int | None:
    agent = await _find_agent(session, agent_id)
    if agent is None or agent.tenant_id != workspace_id:
        return None
    return await _version_sdlc(session, agent, workspace_id)


async def _first_step(
    session: AsyncSession, workspace_id: int, sdlc_id: int | None
) -> SdlcStep | None:
    if sdlc_id is None:
        return None
    result = await session.scalars(
        select(SdlcStep).where(SdlcStep.sdlc_id == sdlc_id, SdlcStep.is_deleted == 0)
    )
    chosen: SdlcStep | None = None
    for step in result.all():
        if step.tenant_id != workspace_id or step.id is None:
            continue
        if chosen is None or step.step_order < chosen.step_order:
            chosen = step
    return chosen


async def _require_comment(
    session: AsyncSession, workspace_id: int, workitem_id: int, comment_id: int
) -> WorkitemComment:
    comment = await session.scalar(
        select(WorkitemComment)
        .where(
            WorkitemComment.tenant_id == workspace_id,
            WorkitemComment.source_type == "WORKITEM",
            WorkitemComment.id == comment_id,
        )
        .limit(1)
    )
    if comment is None or comment.workitem_id != workitem_id:
        raise IllegalArgumentError("guidance comment does not belong to source")
    return comment


async def _interaction(
    session: AsyncSession,
    workspace_id: int,
    guidance: WorkitemCommentDelivery,
    comments: dict[int, TimelineItemView],
    nested: set[int],
) -> CommentInteractionView:
    target = await session.scalar(
        select(Agent).where(Agent.id == guidance.target_agent_id, Agent.is_deleted == 0).limit(1)
    )
    name = str(guidance.target_agent_id)
    if target is not None and target.name is not None:
        name = target.name
    view = CommentInteractionView(
        guidance_id=guidance.id,
        target_agent_id=guidance.target_agent_id,
        target_agent_name=name,
        status=guidance.status,
        error=guidance.error,
        dispatch_id=guidance.dispatch_id,
    )
    execution = None
    if guidance.dispatch_id is not None:
        execution = await session.scalar(
            select(Dispatch).where(Dispatch.id == guidance.dispatch_id).limit(1)
        )
    if execution is not None and execution.tenant_id == workspace_id:
        view.execution_status = execution.status
        if guidance.status in {"QUEUED", "DELIVERED"} and execution.status in _FAILED_TURN:
            view.status = "FAILED"
            if execution.status == "CANCELED":
                view.status = "CANCELED"
            view.error = execution.error
    if guidance.reply_comment_id is not None:
        reply = comments.get(guidance.reply_comment_id)
        if reply is not None and reply.id is not None:
            view.reply_comment_id = reply.id
            view.reply_content = reply.content
            view.replied_at = reply.gmt_create
            nested.add(reply.id)
    return view


async def _bind_reply(
    session: AsyncSession, guidance_id: int, workspace_id: int, reply_id: int
) -> int:
    result = await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.id == guidance_id,
            WorkitemCommentDelivery.tenant_id == workspace_id,
            WorkitemCommentDelivery.reply_comment_id.is_(None),
        )
        .values(reply_comment_id=reply_id, gmt_modified=now_local())
    )
    return rowcount(result)


async def _bind_dispatch(
    session: AsyncSession, guidance_id: int, workspace_id: int, dispatch_id: int
) -> int:
    result = await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.id == guidance_id,
            WorkitemCommentDelivery.tenant_id == workspace_id,
            WorkitemCommentDelivery.status == "QUEUED",
        )
        .values(dispatch_id=dispatch_id, gmt_modified=now_local())
    )
    return rowcount(result)


async def _set_status(
    session: AsyncSession,
    guidance_id: int,
    workspace_id: int,
    status: str,
    error: str | None,
) -> None:
    values: dict[str, object] = {
        "status": status,
        "error": error,
        "gmt_modified": now_local(),
    }
    if status == "APPLIED":
        values["applied_at"] = now_local()
    if status == "DELIVERED":
        values["delivered_at"] = now_local()
    await session.execute(
        update(WorkitemCommentDelivery)
        .where(
            WorkitemCommentDelivery.id == guidance_id,
            WorkitemCommentDelivery.tenant_id == workspace_id,
        )
        .values(**values)
    )
