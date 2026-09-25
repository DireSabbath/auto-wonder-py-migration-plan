"""派发 integration_outbox。Aone 关闭时跳过 AONE 行，其他提供商仍会发送。"""

import logging
from datetime import timedelta

import httpx
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
from autowonder.integrations.comment_outbound import outbound_comment_text
from autowonder.integrations.generic_writeback import update_generic_content
from autowonder.integrations.models import (
    ExternalCommentLink,
    ExternalProjectBinding,
    IntegrationOutbox,
)
from autowonder.integrations.receipts_sanitize import sanitize_error

logger = logging.getLogger(__name__)

_CLIENT = AoneClient()
_MAX_RETRIES = 10
_MISSING_STAFF = "Aone writeback staffId is required"


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
    if not _same_provider(item.provider, binding.provider):
        await _fail(session, item, False, "binding provider mismatch")
        return False
    aone = _same_provider(PROVIDER, item.provider)
    generic_content = (not aone) and item.event_type == "CONTENT_UPDATE"
    if not aone and not generic_content:
        await _fail(session, item, False, "provider not supported: " + item.provider)
        return False
    if item.event_type == "STATUS_UPDATE_SKIPPED":
        await _fail(session, item, False, "status update skipped: missing mapping")
        return False
    if _requires_staff(item) and _blank(binding.writeback_staff_id):
        await _fail(session, item, True, _MISSING_STAFF)
        return False
    payload = item.payload_json if isinstance(item.payload_json, dict) else {}
    config = AoneConfig(
        binding.base_url,
        binding.client_key,
        _credential(binding.credential_ref),
        binding.region_id,
    )
    external_effect = False
    try:
        if item.event_type == "COMMENT_CREATE":
            content = await outbound_comment_text(session, item, payload)
            comment = create_comment(
                _CLIENT,
                config,
                _payload_text(payload, "externalWorkitemId"),
                binding.writeback_staff_id,
                content,
            )
            external_effect = True
            if comment.external_id is not None:
                source_status = "ACTIVE"
                if comment.source_status is not None and comment.source_status.strip() != "":
                    source_status = comment.source_status
                external_workitem_id = _payload_text(payload, "externalWorkitemId")
                if external_workitem_id is None:
                    external_workitem_id = ""
                session.add(
                    ExternalCommentLink(
                        tenant_id=item.tenant_id,
                        provider=item.provider,
                        binding_id=item.binding_id,
                        external_workitem_id=external_workitem_id,
                        external_comment_id=comment.external_id,
                        workitem_comment_id=_payload_long(payload, "commentId"),
                        direction="OUTBOUND",
                        source_updated_at=comment.updated_at,
                        source_status=source_status,
                    )
                )
        elif item.event_type == "STATUS_UPDATE":
            update_status(
                _CLIENT,
                config,
                _payload_text(payload, "externalWorkitemId"),
                binding.writeback_staff_id,
                _payload_text(payload, "externalStatusName"),
            )
            external_effect = True
        elif item.event_type == "CONTENT_UPDATE":
            title = _payload_text(payload, "title")
            content_md = _payload_text(payload, "contentMd")
            external_id = _payload_text(payload, "externalWorkitemId")
            if generic_content:
                update_generic_content(item.provider, config, external_id, title, content_md)
            else:
                update_content(
                    _CLIENT,
                    config,
                    external_id,
                    binding.writeback_staff_id,
                    title,
                    content_md,
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
        if external_effect or _ambiguous(error):
            await _mark_unknown(session, item, error)
        else:
            await _fail(session, item, _retryable(item, error), _error_message(error))
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
            last_error=sanitize_error(_error_message(error)),
            next_retry_at=None,
            gmt_modified=now_local(),
        )
    )


def _retryable(item: IntegrationOutbox, error: BaseException) -> bool:
    return (not _terminal(error)) and (not _exhausted(item))


def _exhausted(item: IntegrationOutbox) -> bool:
    current = 0 if item.retry_count is None else item.retry_count
    return current + 1 >= _MAX_RETRIES


def _terminal(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, AoneOpenApiError) and cause.terminal:
            return True
        cause = cause.__cause__
    return False


def _ambiguous(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, OSError | TimeoutError | httpx.HTTPError):
            return True
        if isinstance(cause, AoneOpenApiError) and str(cause).startswith(
            "Aone returned non-JSON response"
        ):
            return True
        cause = cause.__cause__
    return False


def _requires_staff(item: IntegrationOutbox) -> bool:
    if not _same_provider(PROVIDER, item.provider):
        return False
    return item.event_type in {"COMMENT_CREATE", "STATUS_UPDATE", "CONTENT_UPDATE"}


def _same_provider(left: str | None, right: str | None) -> bool:
    return _provider_key(left) == _provider_key(right)


def _provider_key(provider: str | None) -> str:
    if provider is None:
        return ""
    return provider.strip().upper()


def _credential(credential_ref: str | None) -> str:
    if credential_ref is None or credential_ref.strip() == "":
        return ""
    return crypto().decrypt(credential_ref)


def _payload_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return str(value)


def _payload_long(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _blank(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _error_message(error: BaseException) -> str:
    text = str(error)
    if text.strip() == "":
        return type(error).__name__
    return text
