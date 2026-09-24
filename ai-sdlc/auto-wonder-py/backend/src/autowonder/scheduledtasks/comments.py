"""定时任务运行上的数字员工评论和真人提及。"""

import logging
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.core.errors import BizError, ErrorCode, IllegalArgumentError
from autowonder.debuglogs.sanitizer import java_is_blank, java_is_whitespace
from autowonder.notifications.service import publish
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.scheduledtasks.models import ScheduledTask, ScheduledTaskRun
from autowonder.scheduledtasks.notify import publish_comment
from autowonder.users.models import User
from autowonder.workitems.models import WorkitemComment, WorkitemCommentMention
from autowonder.workitems.schemas import CommentView
from autowonder.workitems.view import person_name
from autowonder.workspaces.models import OrgMember

logger = logging.getLogger(__name__)


@dataclass
class RunMention:
    """运行评论提交后要发给被 @ 真人的站内通知。"""

    workspace_id: int
    run_id: int
    title: str
    comment_id: int
    recipient_user_id: int
    actor_display_name: str
    content_md: str


async def list_run_comments(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
) -> list[CommentView]:
    """列出一次运行上的评论。运行不存在按工单不存在处理。"""
    require_scheduled_capability()
    await _require_run(session, workspace_id, run_id)
    rows = await session.scalars(
        select(WorkitemComment)
        .where(
            WorkitemComment.tenant_id == workspace_id,
            WorkitemComment.source_type == "SCHEDULED_TASK_RUN",
            WorkitemComment.workitem_id == run_id,
        )
        .order_by(WorkitemComment.id.asc())
    )
    return [_comment_view(row) for row in rows.all()]


async def add_human_comment(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
    user_id: int,
    content_md: str | None,
    explicit_target_agent_ids: list[int],
    explicit_target_human_ids: list[int],
) -> tuple[CommentView, list[RunMention]]:
    """给运行写真人评论。空白正文是参数不合法。"""
    require_scheduled_capability()
    run = await _require_run(session, workspace_id, run_id)
    if java_is_blank(content_md):
        raise BizError(ErrorCode.PARAM_INVALID)
    comment = WorkitemComment(
        tenant_id=workspace_id,
        source_type="SCHEDULED_TASK_RUN",
        workitem_id=run_id,
        author_type="HUMAN",
        author_ref=user_id,
        content_md=content_md,
    )
    session.add(comment)
    await session.flush()
    user = await session.get(User, user_id)
    display = "HUMAN"
    if user is not None:
        display = person_name(user.nickname, user.username)
    notices = await _mentions(
        session,
        workspace_id,
        run,
        comment,
        user_id,
        explicit_target_agent_ids,
        explicit_target_human_ids,
        display,
    )
    await publish_comment(session, workspace_id, run_id, comment.id)
    return _comment_view(comment), notices


async def add_run_agent_comment(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
    agent_id: int,
    content_md: str,
    explicit_target_agent_ids: list[int],
    explicit_target_human_ids: list[int],
) -> tuple[CommentView, list[RunMention]]:
    """给一次定时任务运行写数字员工评论。显式目标优先于正文开头的 @。"""
    require_scheduled_capability()
    run = await _require_run(session, workspace_id, run_id)
    if java_is_blank(content_md):
        raise BizError(ErrorCode.PARAM_INVALID)
    comment = WorkitemComment(
        tenant_id=workspace_id,
        source_type="SCHEDULED_TASK_RUN",
        workitem_id=run_id,
        author_type="AGENT",
        author_ref=agent_id,
        content_md=content_md,
    )
    session.add(comment)
    await session.flush()
    notices = await _mentions(
        session,
        workspace_id,
        run,
        comment,
        agent_id,
        explicit_target_agent_ids,
        explicit_target_human_ids,
    )
    await publish_comment(session, workspace_id, run_id, comment.id)
    return _comment_view(comment), notices


async def publish_run_mentions(session: AsyncSession, notices: list[RunMention]) -> None:
    """评论提交后再发站内通知。单条失败只记日志。"""
    for notice in notices:
        try:
            summary = _truncate(notice.content_md, 100)
            await publish(
                session,
                notice.workspace_id,
                "COMMENT_MENTION",
                "有人在定时任务评论中@了你",
                notice.actor_display_name
                + " 在定时任务「"
                + notice.title
                + "」的执行记录评论中@了你："
                + summary,
                "/scheduled-task-runs/" + str(notice.run_id),
                "SCHEDULED_TASK_RUN",
                notice.run_id,
                [notice.recipient_user_id],
            )
        except Exception:
            logger.exception(
                "failed to send in-app notification for comment mention"
                " tenantId=%s workitemId=%s recipient=%s",
                notice.workspace_id,
                notice.run_id,
                notice.recipient_user_id,
            )


async def _mentions(
    session: AsyncSession,
    workspace_id: int,
    run: ScheduledTaskRun,
    comment: WorkitemComment,
    creator_id: int,
    explicit_target_agent_ids: list[int],
    explicit_target_human_ids: list[int],
    actor_display: str | None = None,
) -> list[RunMention]:
    agent_targets = _distinct(explicit_target_agent_ids)
    human_targets = _distinct(explicit_target_human_ids)
    title = await _run_title(session, workspace_id, run)
    display = await _actor_display(session, creator_id) if actor_display is None else actor_display
    if len(agent_targets) > 0:
        frozen = _frozen_agent_ids(run.execution_snapshot_json)
        for target_id in agent_targets:
            agent = await session.scalar(
                select(Agent).where(Agent.id == target_id, Agent.is_deleted == 0).limit(1)
            )
            if agent is None or agent.tenant_id != workspace_id or target_id not in frozen:
                raise IllegalArgumentError(
                    "guidance target is not a frozen scheduled-run participant"
                )
            await _persist_agent(session, workspace_id, run.id, comment.id, agent)
    notices: list[RunMention] = []
    for user_id in human_targets:
        user = await _require_human(session, workspace_id, user_id)
        await _persist_human(session, workspace_id, run.id, comment.id, user)
        if user_id != creator_id:
            notices.append(
                RunMention(
                    workspace_id,
                    run.id,
                    title,
                    comment.id,
                    user_id,
                    display,
                    comment.content_md or "",
                )
            )
    if len(agent_targets) > 0 or len(human_targets) > 0:
        return notices
    name = _leading_mention(comment.content_md or "")
    if name is None:
        return notices
    named = await session.scalars(
        select(Agent).where(
            Agent.tenant_id == workspace_id,
            Agent.name == name,
            Agent.is_deleted == 0,
        )
    )
    agents = [agent for agent in named.all() if agent.id is not None]
    frozen = _frozen_agent_ids(run.execution_snapshot_json)
    if len(agents) == 1 and agents[0].id in frozen:
        await _persist_agent(session, workspace_id, run.id, comment.id, agents[0])
        return notices
    human = await _match_human(session, workspace_id, name)
    if human is None or human.id is None:
        return notices
    await _persist_human(session, workspace_id, run.id, comment.id, human)
    if human.id != creator_id:
        notices.append(
            RunMention(
                workspace_id,
                run.id,
                title,
                comment.id,
                human.id,
                display,
                comment.content_md or "",
            )
        )
    return notices


async def _require_run(session: AsyncSession, workspace_id: int, run_id: int) -> ScheduledTaskRun:
    run = await session.scalar(
        select(ScheduledTaskRun)
        .where(
            ScheduledTaskRun.workspace_id == workspace_id,
            ScheduledTaskRun.id == run_id,
        )
        .limit(1)
    )
    if run is None or run.workspace_id != workspace_id:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    return run


async def _require_human(session: AsyncSession, workspace_id: int, user_id: int) -> User:
    member = await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
    if member is None or member.status != 0:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    user = await session.scalar(
        select(User).where(User.id == user_id, User.is_deleted == 0).limit(1)
    )
    if user is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return user


async def _match_human(session: AsyncSession, workspace_id: int, name: str) -> User | None:
    if java_is_blank(name):
        return None
    found = await session.scalars(
        select(User)
        .where(
            User.is_deleted == 0,
            User.status == 0,
            or_(User.username == name, User.nickname == name),
        )
        .limit(3)
    )
    matches = [user for user in found.all() if user.id is not None]
    if len(matches) != 1:
        return None
    user = matches[0]
    member = await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user.id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
    if member is None or member.status != 0:
        return None
    return user


async def _persist_human(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
    comment_id: int,
    user: User,
) -> None:
    session.add(
        WorkitemCommentMention(
            tenant_id=workspace_id,
            source_type="SCHEDULED_TASK_RUN",
            workitem_id=run_id,
            comment_id=comment_id,
            target_type="HUMAN",
            target_ref=user.id,
            display_name_snapshot=person_name(user.nickname, user.username),
        )
    )
    await session.flush()


async def _persist_agent(
    session: AsyncSession,
    workspace_id: int,
    run_id: int,
    comment_id: int,
    agent: Agent,
) -> None:
    session.add(
        WorkitemCommentMention(
            tenant_id=workspace_id,
            source_type="SCHEDULED_TASK_RUN",
            workitem_id=run_id,
            comment_id=comment_id,
            target_type="AGENT",
            target_ref=agent.id,
            display_name_snapshot=agent.name,
        )
    )
    await session.flush()


async def _run_title(session: AsyncSession, workspace_id: int, run: ScheduledTaskRun) -> str:
    task = await session.scalar(
        select(ScheduledTask)
        .where(
            ScheduledTask.workspace_id == workspace_id,
            ScheduledTask.id == run.scheduled_task_id,
            ScheduledTask.is_deleted == 0,
        )
        .limit(1)
    )
    if task is not None and not java_is_blank(task.name):
        return task.name
    return "定时任务运行 #" + str(run.id)


async def _actor_display(session: AsyncSession, agent_id: int) -> str:
    agent = await session.scalar(
        select(Agent).where(Agent.id == agent_id, Agent.is_deleted == 0).limit(1)
    )
    if agent is not None and agent.name is not None and not java_is_blank(agent.name):
        return agent.name
    return "AGENT"


def _frozen_agent_ids(snapshot: object) -> set[int]:
    if not isinstance(snapshot, dict):
        return set()
    contexts = snapshot.get("agentContexts")
    if not isinstance(contexts, list):
        return set()
    agent_ids: set[int] = set()
    for context in contexts:
        if not isinstance(context, dict):
            continue
        agent_id = context.get("agentId")
        if isinstance(agent_id, bool) or not isinstance(agent_id, int):
            continue
        agent_ids.add(agent_id)
    return agent_ids


def _leading_mention(content: str) -> str | None:
    chars = list(content)
    start = 0
    while start < len(chars) and java_is_whitespace(ord(chars[start])):
        start += 1
    if start >= len(chars) or chars[start] != "@":
        return None
    end = start + 1
    while end < len(chars) and not java_is_whitespace(ord(chars[end])):
        end += 1
    name = _java_trim("".join(chars[start + 1 : end]))
    if java_is_blank(name):
        return None
    return name


def _java_trim(value: str) -> str:
    chars = list(value)
    start = 0
    end = len(chars)
    while start < end and ord(chars[start]) <= 0x20:
        start += 1
    while end > start and ord(chars[end - 1]) <= 0x20:
        end -= 1
    return "".join(chars[start:end])


def _distinct(values: list[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _comment_view(comment: WorkitemComment) -> CommentView:
    return CommentView(
        id=comment.id,
        workitem_id=comment.workitem_id,
        author_type=comment.author_type,
        author_ref=comment.author_ref,
        content_md=comment.content_md,
        gmt_create=comment.gmt_create,
    )


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
