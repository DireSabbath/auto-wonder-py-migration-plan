"""工单写出后交给调度、通知和集成的事件。"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.context import current_request_id
from autowonder.debuglogs.sanitizer import java_is_blank

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkitemAssigned:
    """指派给数字员工。调度监听在步骤和员工都存在时入队。"""

    tenant_id: int
    workitem_id: int
    sdlc_step_id: int | None
    agent_id: int | None
    assignment_version: int
    user_id: int


@dataclass(frozen=True)
class WorkitemHumanAssigned:
    """指派给另一位真人。站内通知和当前 IM 一起发出。"""

    tenant_id: int
    workitem_id: int
    workitem_title: str
    workitem_event_id: int
    recipient_user_id: int
    actor_type: str
    actor_ref: int
    actor_display_name: str
    request_id: str | None


@dataclass(frozen=True)
class WorkitemStatusChanged:
    """状态流转。"""

    actor_type: str
    tenant_id: int
    workitem_id: int
    to_node_id: int
    user_id: int


@dataclass(frozen=True)
class WorkitemContentUpdated:
    """标题或正文已改。"""

    tenant_id: int
    workitem_id: int
    title: str
    content_md: str | None
    user_id: int


async def publish_human_assigned(
    session: AsyncSession,
    event: WorkitemHumanAssigned,
) -> None:
    """给被指派的真人发站内通知，并按偏好发当前 IM。"""
    from autowonder.notifications.service import publish

    logger.info(
        "workitem human assigned workitemId=%s recipient=%s",
        event.workitem_id,
        event.recipient_user_id,
    )
    try:
        await publish(
            session,
            event.tenant_id,
            "WORKITEM_ASSIGNED",
            "工单已指派给你",
            event.actor_display_name + " 将「" + event.workitem_title + "」指派给了你",
            "/workitems/" + str(event.workitem_id),
            "WORKITEM",
            event.workitem_id,
            [event.recipient_user_id],
        )
    except Exception:
        logger.exception(
            "failed to notify workitem assignment workitemId=%s recipient=%s",
            event.workitem_id,
            event.recipient_user_id,
        )
        await session.rollback()


def publish_status_changed(event: WorkitemStatusChanged) -> None:
    """状态变更事件。集成回写随外部平台迁移。"""
    logger.info(
        "workitem status changed workitemId=%s toNodeId=%s",
        event.workitem_id,
        event.to_node_id,
    )


def publish_content_updated(event: WorkitemContentUpdated) -> None:
    """内容变更事件。集成回写随外部平台迁移。"""
    logger.info("workitem content updated workitemId=%s", event.workitem_id)


def request_id_or_none() -> str | None:
    """空白请求号按 Java 写成 null。"""
    request_id = current_request_id()
    if request_id is None or java_is_blank(request_id):
        return None
    return request_id
