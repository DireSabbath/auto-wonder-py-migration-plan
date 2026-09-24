"""工单事件时间线和统一时间线。外部协作快照尚未接入。"""

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.workitems.models import Workitem, WorkitemComment, WorkitemEvent
from autowonder.workitems.schemas import EventView, TimelineItemView
from autowonder.workitems.service import _actor_name

_LABELS = {
    "CREATE": "工单已创建",
    "STATUS_CHANGE": "工单状态已变更",
    "ASSIGN": "交付负责人已变更",
    "EDIT": "工单内容已更新",
    "DELETE": "工单已删除",
    "COMMENT": "新增评论",
    "AONE_IMPORT": "已从 Aone 工单导入",
    "AONE_UPDATE": "已从 Aone 工单同步更新",
    "EXTERNAL_IMPORT": "已从外部工单导入",
    "EXTERNAL_UPDATE": "已从外部工单同步更新",
    "EXTERNAL_BUSINESS_OWNER_CHANGE": "外部业务负责人已变更",
    "EXTERNAL_LIFECYCLE_CHANGE": "来源工单生命周期已变更",
    "EXTERNAL_COMMENT_EDIT": "外部评论信息已更新",
    "EXTERNAL_COMMENT_AUTHOR_CHANGE": "外部评论作者身份已更新",
    "EXTERNAL_COMMENT_DELETE": "外部评论已删除",
}
_COMMENT_EVENTS = {
    "EXTERNAL_COMMENT_EDIT",
    "EXTERNAL_COMMENT_AUTHOR_CHANGE",
    "EXTERNAL_COMMENT_DELETE",
}


async def timeline(session: AsyncSession, workitem_id: int) -> list[EventView]:
    """按 id 升序返回事件。展示名在读出后解析。"""
    result = await session.scalars(
        select(WorkitemEvent).where(WorkitemEvent.workitem_id == workitem_id)
    )
    rows = list(result.all())
    rows.sort(key=lambda row: row.id)
    views: list[EventView] = []
    for event in rows:
        name = await _actor_name(session, event.actor_type, event.actor_ref)
        views.append(
            EventView(
                id=event.id,
                event_type=event.event_type,
                from_val=event.from_val,
                to_val=event.to_val,
                actor_type=event.actor_type,
                actor_ref=event.actor_ref,
                actor_name=name,
                actor_display_name=await _display(session, event.actor_type, event.actor_ref),
                from_val_display=await _value_display(session, event, event.from_val, "fromType"),
                to_val_display=await _value_display(session, event, event.to_val, "toType"),
                detail_json=_detail_text(event.detail_json),
                gmt_create=event.gmt_create,
            )
        )
    return views


async def unified_timeline(session: AsyncSession, workitem_id: int) -> list[TimelineItemView]:
    """评论和系统事件混在一起，新的在前。"""
    owner = await session.scalar(
        select(Workitem).where(Workitem.id == workitem_id, Workitem.is_deleted == 0).limit(1)
    )
    if owner is None or owner.tenant_id is None:
        raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
    items: list[TimelineItemView] = []
    comments = await session.scalars(
        select(WorkitemComment).where(
            WorkitemComment.tenant_id == owner.tenant_id,
            WorkitemComment.source_type == "WORKITEM",
            WorkitemComment.workitem_id == workitem_id,
        )
    )
    for comment in comments.all():
        items.append(
            TimelineItemView(
                id=comment.id,
                type="comment",
                author_id=comment.author_ref,
                author_type=comment.author_type,
                agent=comment.author_type == "AGENT",
                content=comment.content_md,
                gmt_create=comment.gmt_create,
                author_name=await _display(session, comment.author_type, comment.author_ref),
            )
        )
    events = await session.scalars(
        select(WorkitemEvent).where(WorkitemEvent.workitem_id == workitem_id)
    )
    for event in events.all():
        from_val = await _value_display(session, event, event.from_val, "fromType")
        to_val = await _value_display(session, event, event.to_val, "toType")
        content = await _append_operator(
            session, _format_content(event, from_val, to_val), event
        )
        items.append(
            TimelineItemView(
                id=event.id,
                type="system",
                author_id=event.actor_ref,
                author_type=event.actor_type,
                agent=event.actor_type == "AGENT",
                gmt_create=event.gmt_create,
                author_name=await _display(session, event.actor_type, event.actor_ref),
                content=content,
            )
        )
    items.sort(key=lambda item: item.id or 0, reverse=True)
    items.sort(key=lambda item: item.gmt_create or datetime.min, reverse=True)
    return items


def _format_content(event: WorkitemEvent, from_val: str | None, to_val: str | None) -> str:
    label = _LABELS.get(event.event_type, event.event_type)
    if event.event_type in {"AONE_IMPORT", "AONE_UPDATE"}:
        return label
    if event.event_type in _COMMENT_EVENTS:
        if event.from_val is None or java_is_blank(event.from_val):
            return label
        return label + "（评论 #" + event.from_val + "）"
    if event.from_val is not None and event.to_val is not None:
        return label + ": " + _text(from_val) + " → " + _text(to_val)
    if event.to_val is not None:
        return label + ": " + _text(to_val)
    return label


async def _append_operator(
    session: AsyncSession, content: str, event: WorkitemEvent
) -> str:
    operator = await _operator(session, event)
    if operator is None:
        return content
    return content + " （操作人：" + operator + "）"


async def _operator(session: AsyncSession, event: WorkitemEvent) -> str | None:
    if event.event_type not in {"STATUS_CHANGE", "ASSIGN"}:
        return None
    if event.actor_type is None or event.actor_type == "SYSTEM":
        return None
    if event.actor_ref is None or event.actor_ref == 0:
        return None
    display = await _display(session, event.actor_type, event.actor_ref)
    if display is None or java_is_blank(display):
        return None
    return display


async def _display(
    session: AsyncSession, actor_type: str | None, actor_ref: int | None
) -> str | None:
    name = await _actor_name(session, actor_type, actor_ref)
    if actor_ref is None:
        return name
    if name is None or java_is_blank(name):
        return str(actor_ref)
    return name + "(" + str(actor_ref) + ")"


async def _value_display(
    session: AsyncSession, event: WorkitemEvent, value: str | None, type_key: str
) -> str | None:
    if value is None or java_is_blank(value):
        return value
    if event.event_type not in {"ASSIGN", "EXTERNAL_BUSINESS_OWNER_CHANGE"}:
        return value
    try:
        ref = int(value)
    except ValueError:
        return value
    if event.event_type == "EXTERNAL_BUSINESS_OWNER_CHANGE":
        return value
    explicit = _detail_type(event.detail_json, type_key)
    if explicit is not None:
        display = await _display(session, explicit, ref)
        if display is None:
            return value
        return display
    agent_display = await _display(session, "AGENT", ref)
    if agent_display is not None and agent_display != value:
        return agent_display
    human_display = await _display(session, "HUMAN", ref)
    if human_display is None:
        return value
    return human_display


def _detail_type(detail: object, key: str) -> str | None:
    parsed = _detail_object(detail)
    if parsed is None:
        return None
    value = parsed.get(key)
    if value in {"AGENT", "HUMAN"}:
        return value
    return None


def _detail_text(detail: object) -> str | None:
    if detail is None:
        return None
    if isinstance(detail, str):
        return detail
    return json.dumps(detail, ensure_ascii=False, separators=(",", ":"))


def _detail_object(detail: object) -> dict[str, object] | None:
    if detail is None:
        return None
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str):
        if java_is_blank(detail):
            return None
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _text(value: str | None) -> str:
    if value is None:
        return ""
    return value
