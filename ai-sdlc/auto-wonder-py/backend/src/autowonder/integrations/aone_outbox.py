"""派发 integration_outbox。Aone 关闭时跳过 AONE 行，其他提供商仍会发送。"""

import logging
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from autowonder.core.clock import now_local
from autowonder.db.rows import rowcount
from autowonder.integrations.aone_api import (
    AoneClient,
    AoneConfig,
    create_comment,
    update_content,
    update_status,
)
from autowonder.integrations.aone_codec import AoneOpenApiError, aone_enabled
from autowonder.integrations.aone_service import PROVIDER, crypto
from autowonder.integrations.models import (
    ExternalCommentLink,
    ExternalProjectBinding,
    IntegrationOutbox,
)
from autowonder.integrations.receipts_sanitize import sanitize_error

logger = logging.getLogger(__name__)

_CLIENT = AoneClient()
_MAX_RETRIES = 8
_MISSING_STAFF = "missing writeback staff id"


async def dispatch_pending(session: AsyncSession, limit: int) -> int:
    """抢占待发送行并回写。返回成功条数。"""
    if aone_enabled():
        statement = (
            select(IntegrationOutbox).where(_pending()).order_by(IntegrationOutbox.id.asc())
        )
    else:
        statement = (
            select(IntegrationOutbox)
            .where(IntegrationOutbox.provider != PROVIDER, _pending())
            .order_by(IntegrationOutbox.id.asc())
        )
    rows = list(await session.scalars(statement.limit(limit)))
    success = 0
    for item in rows:
        if await _dispatch_one(session, item):
            success += 1
    return success


def _pending() -> ColumnElement[bool]:
    return or_(
        IntegrationOutbox.status == "PENDING",
        (IntegrationOutbox.status == "FAILED") & (IntegrationOutbox.next_retry_at <= now_local()),
    )


async def _dispatch_one(session: AsyncSession, item: IntegrationOutbox) -> bool:
    expected = 0 if item.lock_version is None else item.lock_version
    claimed = await session.execute(
        update(IntegrationOutbox)
        .where(
            IntegrationOutbox.id == item.id,
            IntegrationOutbox.lock_version == expected,
            _pending(),
        )
        .values(
            status="SENDING",
            lock_version=IntegrationOutbox.lock_version + 1,
            last_error=None,
            next_retry_at=None,
            gmt_modified=now_local(),
        )
    )
    if rowcount(claimed) != 1:
        return False
    item.lock_version = expected + 1
    item.status = "SENDING"
    binding = await session.get(ExternalProjectBinding, item.binding_id)
    if binding is None:
        await _fail(session, item, False, "binding not found")
        return False
    if item.tenant_id != binding.tenant_id:
        await _fail(session, item, False, "binding tenant mismatch")
        return False
    if item.provider != binding.provider:
        await _fail(session, item, False, "binding provider mismatch")
        return False
    if item.provider != PROVIDER:
        await _fail(session, item, False, "provider not supported: " + item.provider)
        return False
    if item.event_type == "STATUS_UPDATE_SKIPPED":
        await _fail(session, item, False, "status update skipped: missing mapping")
        return False
    if item.event_type in {"COMMENT_CREATE", "STATUS_UPDATE", "CONTENT_UPDATE"} and (
        binding.writeback_staff_id is None or binding.writeback_staff_id.strip() == ""
    ):
        await _fail(session, item, True, _MISSING_STAFF)
        return False
    payload = item.payload_json if isinstance(item.payload_json, dict) else {}
    config = AoneConfig(
        binding.base_url,
        binding.client_key,
        crypto().decrypt(binding.credential_ref),
        binding.region_id,
    )
    external_effect = False
    try:
        if item.event_type == "COMMENT_CREATE":
            comment = create_comment(
                _CLIENT,
                config,
                str(payload.get("externalWorkitemId") or ""),
                binding.writeback_staff_id,
                str(payload.get("content") or ""),
            )
            external_effect = True
            if comment.external_id is not None:
                session.add(
                    ExternalCommentLink(
                        tenant_id=item.tenant_id,
                        provider=item.provider,
                        binding_id=item.binding_id,
                        external_workitem_id=str(payload.get("externalWorkitemId") or ""),
                        external_comment_id=comment.external_id,
                        workitem_comment_id=int(payload.get("commentId") or 0),
                        direction="OUTBOUND",
                        source_status="ACTIVE",
                    )
                )
        elif item.event_type == "STATUS_UPDATE":
            update_status(
                _CLIENT,
                config,
                str(payload.get("externalWorkitemId") or ""),
                binding.writeback_staff_id,
                str(payload.get("externalStatusName") or ""),
            )
            external_effect = True
        elif item.event_type == "CONTENT_UPDATE":
            update_content(
                _CLIENT,
                config,
                str(payload.get("externalWorkitemId") or ""),
                binding.writeback_staff_id,
                None if payload.get("title") is None else str(payload.get("title")),
                None if payload.get("contentMd") is None else str(payload.get("contentMd")),
            )
            external_effect = True
        marked = await session.execute(
            update(IntegrationOutbox)
            .where(
                IntegrationOutbox.id == item.id,
                IntegrationOutbox.lock_version == item.lock_version,
                IntegrationOutbox.status.in_(("SENDING", "UNKNOWN")),
            )
            .values(
                status="SUCCEEDED",
                last_error=None,
                next_retry_at=None,
                gmt_modified=now_local(),
            )
        )
        return rowcount(marked) == 1
    except Exception as error:
        logger.warning("outbox dispatch failed id=%s error=%s", item.id, error)
        if external_effect:
            await _mark_unknown(session, item, error)
        else:
            await _fail(session, item, _retryable(item, error), str(error))
        return False


async def _fail(
    session: AsyncSession,
    item: IntegrationOutbox,
    retryable: bool,
    message: str,
) -> None:
    delay = None
    if retryable:
        step = min(item.retry_count + 1, 9)
        seconds = min(300, 2**step)
        delay = now_local() + timedelta(seconds=seconds)
    await session.execute(
        update(IntegrationOutbox)
        .where(
            IntegrationOutbox.id == item.id,
            IntegrationOutbox.lock_version == item.lock_version,
            IntegrationOutbox.status == "SENDING",
        )
        .values(
            status="FAILED",
            retry_count=IntegrationOutbox.retry_count + 1,
            last_error=sanitize_error(message),
            next_retry_at=delay,
            gmt_modified=now_local(),
        )
    )


async def _mark_unknown(
    session: AsyncSession,
    item: IntegrationOutbox,
    error: BaseException,
) -> None:
    await session.execute(
        update(IntegrationOutbox)
        .where(
            IntegrationOutbox.id == item.id,
            IntegrationOutbox.lock_version == item.lock_version,
            IntegrationOutbox.status.in_(("SENDING", "UNKNOWN")),
        )
        .values(
            status="UNKNOWN",
            last_error=sanitize_error(str(error)),
            next_retry_at=None,
            gmt_modified=now_local(),
        )
    )


def _retryable(item: IntegrationOutbox, error: BaseException) -> bool:
    if isinstance(error, AoneOpenApiError) and error.terminal:
        return False
    return item.retry_count < _MAX_RETRIES
