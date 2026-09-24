"""工单写出后交给调度、通知和集成的事件。"""

import logging
from dataclasses import dataclass

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
    """指派给另一位真人。IM 通知随渠道迁移。"""

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


def publish_human_assigned(event: WorkitemHumanAssigned) -> None:
    """真人指派通知。发送动作随 IM 迁移，事件本身在这里交出。"""
    logger.info(
        "workitem human assigned workitemId=%s recipient=%s",
        event.workitem_id,
        event.recipient_user_id,
    )


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
