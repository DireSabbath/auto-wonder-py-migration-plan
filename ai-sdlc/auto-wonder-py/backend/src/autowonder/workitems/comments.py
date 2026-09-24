"""工单评论、明文 @ 和提及通知。"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.integrations.comment_outbound import record_outbound_comment
from autowonder.notifications.service import publish
from autowonder.users.models import User
from autowonder.workitems.models import Workitem, WorkitemComment, WorkitemCommentMention
from autowonder.workitems.participants import get_mention_candidates, plain_mention
from autowonder.workitems.schemas import CommentView
from autowonder.workitems.service import _actor_name, _write_event
from autowonder.workitems.view import person_name
from autowonder.workspaces.models import OrgMember

logger = logging.getLogger(__name__)


@dataclass
class MentionNotice:
    """评论提交后要发给被 @ 真人的站内通知。"""

    tenant_id: int
    workitem_id: int
    title: str | None
    comment_id: int
    recipient_user_id: int
    actor_display_name: str | None
    content_md: str | None


async def add_comment(
    session: AsyncSession,
    workitem_id: int,
    content_md: str | None,
    target_human_ids: Sequence[int | None] | None,
    tenant_id: int,
    user_id: int,
) -> tuple[CommentView, list[MentionNotice]]:
    """真人评论。显式提及为空时，从正文里唯一的 @名称 回填。"""
    workitem = await _owned(session, workitem_id, tenant_id)
    humans = await _explicit_humans(session, tenant_id, target_human_ids)
    if len(humans) == 0:
        humans = await _plain_humans(session, workitem_id, tenant_id, content_md)
    comment = WorkitemComment(
        tenant_id=tenant_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        author_type="HUMAN",
        author_ref=user_id,
        content_md=content_md,
    )
    session.add(comment)
    await session.flush()
    await record_outbound_comment(
        session,
        tenant_id,
        workitem_id,
        comment.id,
        "HUMAN",
        user_id,
        content_md,
    )
    await _persist_mentions(session, tenant_id, workitem_id, comment.id, humans)
    await _write_event(
        session, tenant_id, workitem_id, "COMMENT", None, None, "HUMAN", user_id, None
    )
    notices = _notices(
        tenant_id,
        workitem_id,
        workitem.title,
        comment.id,
        humans,
        await _actor_display(session, "HUMAN", user_id),
        user_id,
        content_md,
    )
    return _comment_view(comment), notices


async def add_agent_comment(
    session: AsyncSession,
    workitem_id: int,
    content_md: str | None,
    target_human_ids: Sequence[int | None] | None,
    tenant_id: int,
    agent_id: int,
    initiator_user_id: int | None,
) -> tuple[CommentView, list[MentionNotice]]:
    """数字员工评论。跳过发起这次交互的真人。"""
    workitem = await _owned(session, workitem_id, tenant_id)
    humans = await _explicit_humans(session, tenant_id, target_human_ids)
    if len(humans) == 0:
        humans = await _plain_humans(session, workitem_id, tenant_id, content_md)
    comment = WorkitemComment(
        tenant_id=tenant_id,
        source_type="WORKITEM",
        workitem_id=workitem_id,
        author_type="AGENT",
        author_ref=agent_id,
        content_md=content_md,
    )
    session.add(comment)
    await session.flush()
    await record_outbound_comment(
        session,
        tenant_id,
        workitem_id,
        comment.id,
        "AGENT",
        agent_id,
        content_md,
    )
    await _persist_mentions(session, tenant_id, workitem_id, comment.id, humans)
    await _write_event(
        session, tenant_id, workitem_id, "COMMENT", None, None, "AGENT", agent_id, None
    )
    notices = _notices(
        tenant_id,
        workitem_id,
        workitem.title,
        comment.id,
        humans,
        await _actor_display(session, "AGENT", agent_id),
        initiator_user_id,
        content_md,
    )
    return _comment_view(comment), notices


async def list_comments(session: AsyncSession, workitem_id: int) -> list[CommentView]:
    """按创建时间倒序返回本工单的 WORKITEM 评论。"""
    owner = await _live(session, workitem_id)
    if owner is None or owner.tenant_id is None:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    result = await session.scalars(
        select(WorkitemComment).where(
            WorkitemComment.tenant_id == owner.tenant_id,
            WorkitemComment.source_type == "WORKITEM",
            WorkitemComment.workitem_id == workitem_id,
        )
    )
    rows = list(result.all())
    rows.sort(key=lambda row: ((row.gmt_create is not None), row.gmt_create, row.id), reverse=True)
    return [_comment_view(row) for row in rows]


async def publish_mentions(session: AsyncSession, notices: list[MentionNotice]) -> None:
    """评论事务提交后再发站内通知。单条失败只记日志。"""
    for notice in notices:
        try:
            summary = _truncate(notice.content_md, 100)
            display = notice.actor_display_name
            if display is None:
                display = ""
            title = notice.title
            if title is None:
                title = ""
            await publish(
                session,
                notice.tenant_id,
                "COMMENT_MENTION",
                "有人在评论中@了你",
                display + " 在「" + title + "」@了你：" + summary,
                "/workitems/" + str(notice.workitem_id),
                "WORKITEM",
                notice.workitem_id,
                [notice.recipient_user_id],
            )
        except Exception:
            logger.exception(
                "failed to send in-app notification for comment mention"
                " tenantId=%s workitemId=%s recipient=%s",
                notice.tenant_id,
                notice.workitem_id,
                notice.recipient_user_id,
            )


async def _explicit_humans(
    session: AsyncSession, tenant_id: int, target_human_ids: Sequence[int | None] | None
) -> dict[int, User]:
    humans: dict[int, User] = {}
    if target_human_ids is None:
        return humans
    for target_id in target_human_ids:
        if target_id is None or target_id in humans:
            continue
        member = await session.scalar(
            select(OrgMember)
            .where(
                OrgMember.tenant_id == tenant_id,
                OrgMember.user_id == target_id,
                OrgMember.is_deleted == 0,
            )
            .limit(1)
        )
        if member is None or member.status is None or member.status != 0:
            raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
        user = await session.scalar(
            select(User).where(User.id == target_id, User.is_deleted == 0).limit(1)
        )
        if user is None:
            raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
        humans[target_id] = user
    return humans


async def _plain_humans(
    session: AsyncSession, workitem_id: int, tenant_id: int, content_md: str | None
) -> dict[int, User]:
    humans: dict[int, User] = {}
    if content_md is None or java_is_blank(content_md):
        return humans
    grouped: dict[str, list[int]] = {}
    for candidate in await get_mention_candidates(session, workitem_id, tenant_id, None, 100):
        if candidate.target_type != "HUMAN" or candidate.user_id is None:
            continue
        if candidate.name is None or java_is_blank(candidate.name):
            continue
        grouped.setdefault(candidate.name, []).append(candidate.user_id)
    for name, user_ids in grouped.items():
        if len(user_ids) != 1 or not plain_mention(content_md, name):
            continue
        user = await session.scalar(
            select(User).where(User.id == user_ids[0], User.is_deleted == 0).limit(1)
        )
        if user is not None and user.id not in humans:
            humans[user.id] = user
    return humans


async def _persist_mentions(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    comment_id: int,
    humans: dict[int, User],
) -> None:
    for user_id, user in humans.items():
        session.add(
            WorkitemCommentMention(
                tenant_id=tenant_id,
                source_type="WORKITEM",
                workitem_id=workitem_id,
                comment_id=comment_id,
                target_type="HUMAN",
                target_ref=user_id,
                display_name_snapshot=person_name(user.nickname, user.username),
            )
        )
    if len(humans) > 0:
        await session.flush()


def _notices(
    tenant_id: int,
    workitem_id: int,
    title: str | None,
    comment_id: int,
    humans: dict[int, User],
    actor_display_name: str | None,
    skip_user_id: int | None,
    content_md: str | None,
) -> list[MentionNotice]:
    notices: list[MentionNotice] = []
    for user_id in humans:
        if skip_user_id is not None and user_id == skip_user_id:
            continue
        notices.append(
            MentionNotice(
                tenant_id,
                workitem_id,
                title,
                comment_id,
                user_id,
                actor_display_name,
                content_md,
            )
        )
    return notices


def _comment_view(comment: WorkitemComment) -> CommentView:
    return CommentView(
        id=comment.id,
        workitem_id=comment.workitem_id,
        author_type=comment.author_type,
        author_ref=comment.author_ref,
        content_md=comment.content_md,
        gmt_create=comment.gmt_create,
    )


def _truncate(text: str | None, max_len: int) -> str:
    if text is None:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


async def _owned(session: AsyncSession, workitem_id: int, tenant_id: int) -> Workitem:
    workitem = await _live(session, workitem_id)
    if workitem is None or workitem.tenant_id != tenant_id:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    return workitem


async def _live(session: AsyncSession, workitem_id: int) -> Workitem | None:
    return await session.scalar(
        select(Workitem).where(Workitem.id == workitem_id, Workitem.is_deleted == 0).limit(1)
    )


async def _actor_display(session: AsyncSession, actor_type: str, actor_ref: int) -> str | None:
    name = await _actor_name(session, actor_type, actor_ref)
    if name is None or java_is_blank(name):
        return str(actor_ref)
    return name + "(" + str(actor_ref) + ")"
