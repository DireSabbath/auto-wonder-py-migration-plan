"""评论创建后记下 Aone 写回回执，发送时再按原文核对摘要。"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.integrations.aone_codec import aone_enabled
from autowonder.integrations.aone_sync import PROVIDER
from autowonder.integrations.models import (
    ExternalCommentLink,
    ExternalWorkitemLink,
    IntegrationOutbox,
)
from autowonder.integrations.operation_keys import (
    aone_comment_key,
    operation_marker,
    payload_digest,
    text_digest,
)
from autowonder.integrations.receipts_sanitize import sanitize_json
from autowonder.users.models import User
from autowonder.workitems.models import WorkitemComment

logger = logging.getLogger(__name__)

_SUPPORTED_EVENT = "COMMENT_CREATE"


class ExternalOperationReceiptConflict(RuntimeError):
    """同一个操作键被写成了不同载荷。"""

    def __init__(self, operation_key: str) -> None:
        super().__init__(
            "external operation key was reused with a different payload: " + operation_key
        )


async def record_outbound_comment(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    comment_id: int,
    actor_type: str,
    actor_ref: int,
    content_md: str | None,
) -> None:
    """Aone 打开且工单已关联外部单时，写入不含正文的评论回执。"""
    if not aone_enabled():
        return
    link = await _workitem_link(session, tenant_id, workitem_id)
    if link is None:
        logger.debug(
            "skip outbound comment sync, no external link tenantId=%s workitemId=%s commentId=%s",
            tenant_id,
            workitem_id,
            comment_id,
        )
        return
    if _blocks_outbound(link):
        logger.info(
            "skip outbound comment sync, link blocked tenantId=%s workitemId=%s "
            "commentId=%s sourceLifecycle=%s lastErrorCode=%s",
            tenant_id,
            workitem_id,
            comment_id,
            link.source_lifecycle,
            link.last_error_code,
        )
        return
    linked = await _local_comment_link(session, tenant_id, comment_id)
    if linked is not None:
        logger.debug(
            "skip outbound comment sync, comment already linked tenantId=%s "
            "workitemId=%s commentId=%s",
            tenant_id,
            workitem_id,
            comment_id,
        )
        return
    display_name, source_text = await resolve_actor(session, actor_type, actor_ref)
    content = format_external_comment(display_name, source_text, content_md)
    operation_key = aone_comment_key(workitem_id, comment_id)
    payload = {
        "externalWorkitemId": link.external_workitem_id,
        "commentId": comment_id,
        "contentDigest": text_digest(content),
        "marker": operation_marker(operation_key),
    }
    receipt, created = await begin_receipt(
        session,
        tenant_id,
        PROVIDER,
        link.binding_id,
        workitem_id,
        _SUPPORTED_EVENT,
        operation_key,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    logger.info(
        "recorded outbound comment receipt receiptId=%s created=%s tenantId=%s "
        "workitemId=%s commentId=%s externalWorkitemId=%s",
        receipt.id,
        created,
        tenant_id,
        workitem_id,
        comment_id,
        link.external_workitem_id,
    )


async def outbound_comment_text(
    session: AsyncSession,
    item: IntegrationOutbox,
    payload: dict[str, object],
) -> str:
    """优先用载荷里的旧正文；否则读评论并核对摘要，再补上操作标记。"""
    legacy = payload.get("contentMd")
    marker = _marker(payload)
    if isinstance(legacy, str) and legacy.strip() != "":
        return append_marker(legacy, marker)
    comment_id = payload.get("commentId")
    comment = None
    if isinstance(comment_id, int) and not isinstance(comment_id, bool):
        comment = await session.get(WorkitemComment, comment_id)
    if (
        comment is None
        or comment.tenant_id != item.tenant_id
        or comment.workitem_id != item.workitem_id
    ):
        raise RuntimeError("comment payload source changed or was removed")
    display_name, source_text = await resolve_actor(
        session,
        comment.author_type,
        comment.author_ref,
    )
    content = format_external_comment(display_name, source_text, comment.content_md)
    expected = payload.get("contentDigest")
    if not isinstance(expected, str) or text_digest(content) != expected:
        raise RuntimeError("comment payload source digest changed")
    return append_marker(content, marker)


async def begin_receipt(
    session: AsyncSession,
    tenant_id: int,
    connector: str,
    binding_id: int,
    workitem_id: int,
    event_type: str,
    operation_key: str,
    payload_json: str,
) -> tuple[IntegrationOutbox, bool]:
    """按操作键写入回执。键已存在且载荷一致时返回原行。"""
    _validate_receipt(tenant_id, connector, binding_id, workitem_id, event_type, operation_key)
    provider = connector.strip().upper()
    sanitized = sanitize_json(payload_json)
    existing = await _find_operation(session, tenant_id, provider, binding_id, operation_key)
    if existing is not None:
        return _same_payload(existing, sanitized), False
    receipt = IntegrationOutbox(
        tenant_id=tenant_id,
        provider=provider,
        binding_id=binding_id,
        workitem_id=workitem_id,
        event_type=event_type,
        payload_json=json.loads(sanitized),
        operation_key=operation_key,
        lock_version=0,
        status="PENDING",
        retry_count=0,
    )
    try:
        async with session.begin_nested():
            session.add(receipt)
            await session.flush()
    except IntegrityError:
        winner = await _find_operation(session, tenant_id, provider, binding_id, operation_key)
        if winner is None:
            raise
        return _same_payload(winner, sanitized), False
    return receipt, True


async def resolve_actor(
    session: AsyncSession,
    actor_type: str | None,
    actor_ref: int | None,
) -> tuple[str, str]:
    """把评论作者收成外部评论里的名字和来源行。"""
    if actor_type == "AGENT" and actor_ref is not None:
        agent = await session.get(Agent, actor_ref)
        name = None
        if agent is not None:
            name = _first_present(agent.name, "数字员工")
        display = _named(name, "数字员工")
        return display, "Agent: " + display + "（ID: " + str(actor_ref) + "）"
    if actor_type == "HUMAN" and actor_ref is not None:
        user = await session.get(User, actor_ref)
        name = None
        if user is not None:
            name = _first_present(user.nickname, user.username)
        display = _named(name, "用户")
        return display, "用户: " + display + "（ID: " + str(actor_ref) + "）"
    return "系统", "AutoWonder 系统"


def format_external_comment(
    identity_name: str | None,
    source_text: str | None,
    body: str | None,
) -> str:
    """拼出写到外部系统的评论正文。"""
    name = "系统"
    if identity_name is not None and identity_name.strip() != "":
        name = identity_name.strip()
    source = "AutoWonder 系统"
    if source_text is not None and source_text.strip() != "":
        source = source_text.strip()
    content = ""
    if body is not None:
        content = body.strip()
    return "AutoWonder · " + name + "\n" + "来源：" + source + "\n\n" + content


def append_marker(content: str, marker: str | None) -> str:
    """标记为空或已经在正文里时保持原文。"""
    if marker is None or marker.strip() == "" or marker in content:
        return content
    return content + "\n\n" + marker


def _validate_receipt(
    tenant_id: int,
    connector: str,
    binding_id: int,
    workitem_id: int,
    event_type: str,
    operation_key: str,
) -> None:
    if (
        tenant_id <= 0
        or binding_id <= 0
        or workitem_id <= 0
        or connector.strip() == ""
        or event_type.strip() == ""
        or operation_key.strip() == ""
        or len(operation_key) > 191
    ):
        raise ValueError("external operation request is incomplete")
    if event_type != _SUPPORTED_EVENT:
        raise ValueError("only external comment receipts are supported")


def _same_payload(receipt: IntegrationOutbox, payload_json: str) -> IntegrationOutbox:
    stored = receipt.payload_json
    stored_text = stored if isinstance(stored, str) else json.dumps(stored, ensure_ascii=False)
    if payload_digest(payload_json) != payload_digest(stored_text):
        raise ExternalOperationReceiptConflict(receipt.operation_key)
    return receipt


async def _workitem_link(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
) -> ExternalWorkitemLink | None:
    return await session.scalar(
        select(ExternalWorkitemLink)
        .where(
            ExternalWorkitemLink.tenant_id == tenant_id,
            ExternalWorkitemLink.provider == PROVIDER,
            ExternalWorkitemLink.workitem_id == workitem_id,
        )
        .limit(1)
    )


async def _local_comment_link(
    session: AsyncSession,
    tenant_id: int,
    comment_id: int,
) -> ExternalCommentLink | None:
    return await session.scalar(
        select(ExternalCommentLink)
        .where(
            ExternalCommentLink.tenant_id == tenant_id,
            ExternalCommentLink.workitem_comment_id == comment_id,
        )
        .limit(1)
    )


async def _find_operation(
    session: AsyncSession,
    tenant_id: int,
    provider: str,
    binding_id: int,
    operation_key: str,
) -> IntegrationOutbox | None:
    return await session.scalar(
        select(IntegrationOutbox)
        .where(
            IntegrationOutbox.tenant_id == tenant_id,
            IntegrationOutbox.provider == provider,
            IntegrationOutbox.binding_id == binding_id,
            IntegrationOutbox.operation_key == operation_key,
        )
        .limit(1)
    )


def _blocks_outbound(link: ExternalWorkitemLink) -> bool:
    return (
        link.source_lifecycle == "DELETED"
        or link.source_lifecycle == "UNAVAILABLE"
        or link.last_error_code == "ITEM_FORBIDDEN"
    )


def _marker(payload: dict[str, object]) -> str | None:
    value = payload.get("marker")
    if isinstance(value, str):
        return value
    return None


def _first_present(*values: str | None) -> str | None:
    for value in values:
        if value is not None and value.strip() != "":
            return value
    return None


def _named(value: str | None, fallback: str) -> str:
    if value is not None and value.strip() != "":
        return value
    return fallback
