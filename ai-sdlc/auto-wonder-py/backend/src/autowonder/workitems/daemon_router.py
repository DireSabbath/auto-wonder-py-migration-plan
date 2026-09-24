"""执行器回写工单评论和状态。这条路径在鉴权白名单里，靠执行器令牌校验。"""

import logging
from collections.abc import Collection
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.artifacts.daemon_auth import UploadAuth, authenticate, load_mutation_fence
from autowonder.audits.service import AuditRecord, record_required
from autowonder.core.result import dump_data
from autowonder.db.session import get_session
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.guidance.service import create_for_comment
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.scheduledtasks.comments import add_run_agent_comment, publish_run_mentions
from autowonder.scheduledtasks.notify import scheduled_task_id
from autowonder.workitems.comments import add_agent_comment, publish_mentions
from autowonder.workitems.service import agent_transition

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daemon", tags=["daemon-comments"])

_INTERACTION_ERROR = "interaction dispatch replies are delivered through TASK_GUIDANCE_ACK"
_SCHEDULED_STATUS_ERROR = "scheduled task runs do not support workitem status mutation"


def parse_target_human_ids(raw: object) -> list[int]:
    """只收下数字。布尔值不是 Java ``Number``，浮点按 ``longValue`` 截断。"""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Collection):
        return []
    result: list[int] = []
    for value in raw:
        parsed = _java_long(value)
        if parsed is not None:
            result.append(parsed)
    return result


def daemon_http(status: int, body: object | None) -> Response:
    """没有正文时保持空响应。"""
    if body is None:
        return Response(status_code=status)
    return JSONResponse(status_code=status, content=body)


async def submit_daemon_comment(
    session: AsyncSession,
    dispatch_id: int,
    token: str,
    body: dict[str, Any] | None,
) -> Response:
    """数字员工评论。交互调度和定时任务运行各自拒绝或改写。"""
    auth = await authenticate(session, dispatch_id, token)
    if not auth.success:
        return daemon_http(401, None)
    if await load_mutation_fence(session, dispatch_id):
        return daemon_http(401, None)
    if auth.source_type == "SCHEDULED_TASK_RUN":
        require_scheduled_capability()
    if auth.interaction():
        return daemon_http(409, {"error": _INTERACTION_ERROR})
    content_md = _content_md(body)
    if content_md is None or java_is_blank(content_md):
        return daemon_http(400, {"error": "contentMd required"})
    target_human_ids = parse_target_human_ids(_field(body, "targetHumanIds"))
    if auth.source_type == "SCHEDULED_TASK_RUN":
        view, run_notices = await add_run_agent_comment(
            session,
            auth.tenant_id,
            auth.workitem_id,
            auth.agent_id,
            content_md,
            [],
            target_human_ids,
        )
        logger.info(
            "agent comment added dispatchId=%s workitemId=%s agentId=%s",
            dispatch_id,
            auth.workitem_id,
            auth.agent_id,
        )
        await _record_comment(session, auth, dispatch_id, content_md)
        await session.commit()
        await publish_run_mentions(session, run_notices)
        return daemon_http(200, dump_data(view))
    if auth.source_type == "WORKITEM":
        view, workitem_notices = await add_agent_comment(
            session,
            auth.workitem_id,
            content_md,
            target_human_ids,
            auth.tenant_id,
            auth.agent_id,
            None,
        )
        await create_for_comment(
            session,
            auth.tenant_id,
            auth.workitem_id,
            view.id,
            content_md,
            None,
            auth.agent_id,
        )
        logger.info(
            "agent comment added dispatchId=%s workitemId=%s agentId=%s",
            dispatch_id,
            auth.workitem_id,
            auth.agent_id,
        )
        await _record_comment(session, auth, dispatch_id, content_md)
        await session.commit()
        await publish_mentions(session, workitem_notices)
        return daemon_http(200, dump_data(view))
    return daemon_http(409, None)


async def submit_daemon_workitem_status(
    session: AsyncSession,
    dispatch_id: int,
    token: str,
    body: dict[str, Any] | None,
) -> Response:
    """按状态编码流转工单。定时任务运行明确拒绝，且不改工单。"""
    auth = await authenticate(session, dispatch_id, token)
    if not auth.success:
        return daemon_http(401, None)
    if await load_mutation_fence(session, dispatch_id):
        return daemon_http(401, None)
    if auth.source_type == "SCHEDULED_TASK_RUN":
        require_scheduled_capability()
        return daemon_http(409, {"error": _SCHEDULED_STATUS_ERROR})
    if auth.interaction():
        return daemon_http(409, {"error": _INTERACTION_ERROR})
    status = _status_text(body)
    if status is None or java_is_blank(status):
        return daemon_http(400, {"error": "status required"})
    view = await agent_transition(session, auth.workitem_id, status, auth.tenant_id, auth.agent_id)
    logger.info(
        "agent workitem-status changed dispatchId=%s workitemId=%s agentId=%s status=%s",
        dispatch_id,
        auth.workitem_id,
        auth.agent_id,
        status,
    )
    record = await _daemon_audit(
        session, auth, dispatch_id, "UPDATE_WORKITEM_STATUS", "daemon.workitem-status"
    )
    record.add("status", status)
    await record_required(session, record)
    await session.commit()
    return daemon_http(200, dump_data(view))


async def _record_comment(
    session: AsyncSession, auth: UploadAuth, dispatch_id: int, content_md: str
) -> None:
    record = await _daemon_audit(
        session, auth, dispatch_id, "CREATE_WORKITEM_COMMENT", "daemon.comment"
    )
    record.add("contentLength", len(content_md))
    await record_required(session, record)


async def _daemon_audit(
    session: AsyncSession,
    auth: UploadAuth,
    dispatch_id: int,
    action: str,
    event_type: str,
) -> AuditRecord:
    module = "WORKITEM"
    target_type = "workitem"
    if auth.source_type == "SCHEDULED_TASK_RUN":
        module = "SCHEDULED_TASK"
        target_type = "scheduled_task_run"
    record = AuditRecord(
        tenant_id=auth.tenant_id,
        actor_id=auth.agent_id,
        actor_type="AGENT",
        module=module,
        action=action,
        target_type=target_type,
        target_id=auth.workitem_id,
        trigger_type="EVENT",
        trigger_source="DAEMON_CALLBACK",
        event_type=event_type,
    )
    record.add("dispatchId", dispatch_id)
    record.add("sourceType", auth.source_type)
    if auth.source_type == "SCHEDULED_TASK_RUN":
        record.add("runId", auth.workitem_id)
        record.add("taskId", await scheduled_task_id(session, auth.tenant_id, auth.workitem_id))
    return record


def _content_md(body: dict[str, Any] | None) -> str | None:
    raw = _field(body, "contentMd")
    if isinstance(raw, str):
        return raw
    return None


def _status_text(body: dict[str, Any] | None) -> str | None:
    raw = _field(body, "status")
    if isinstance(raw, str):
        return raw
    return None


def _field(body: dict[str, Any] | None, key: str) -> object:
    if body is None:
        return None
    return body.get(key)


def _java_long(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


@router.post("/dispatches/{dispatchId}/comments")
async def comment(
    dispatchId: int,
    token: Annotated[str, Query()],
    body: Annotated[dict[str, Any] | None, Body()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """接收执行器评论。令牌无效或写入被栅栏挡住时正文为空。"""
    return await submit_daemon_comment(session, dispatchId, token, body)


@router.post("/dispatches/{dispatchId}/workitem-status")
async def workitem_status(
    dispatchId: int,
    token: Annotated[str, Query()],
    body: Annotated[dict[str, Any] | None, Body()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """接收执行器发起的工单状态变更。"""
    return await submit_daemon_workitem_status(session, dispatchId, token, body)
