"""Aone 评论写回后的回读确认。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.integrations.aone_api import (
    AoneClient,
    AoneConfig,
    ExternalComment,
    list_comments,
)
from autowonder.integrations.aone_service import crypto
from autowonder.integrations.models import (
    ExternalCommentLink,
    ExternalProjectBinding,
    IntegrationOutbox,
)
from autowonder.integrations.operation_keys import operation_marker

COMMENT_NOT_FOUND = "Aone comment was not found on readback"
_CLIENT = AoneClient()


def readback_supported(provider: str, event_type: str, enabled: bool) -> bool:
    """只有打开的 Aone 评论创建才去列表里对标记。"""
    return enabled and provider == "AONE" and event_type == "COMMENT_CREATE"


def marker_for_receipt(payload: object, operation_key: str) -> str:
    """载荷里的标记优先，否则按操作键重算。"""
    if isinstance(payload, dict):
        marker = payload.get("marker")
        if isinstance(marker, str) and marker.strip() != "":
            return marker
    return operation_marker(operation_key)


def find_marked_comment(
    comments: list[ExternalComment],
    marker: str,
) -> ExternalComment | None:
    """已删除的评论不算写成功。"""
    for comment in comments:
        if comment.source_status == "DELETED":
            continue
        content = comment.content_md
        if content is None:
            continue
        if marker in content:
            return comment
    return None


async def confirm_comment_readback(
    session: AsyncSession,
    receipt: IntegrationOutbox,
    enabled: bool,
    unavailable: str,
) -> tuple[str, str | None]:
    """找到标记则成功并补出站链接。列表里没有则标成未知。"""
    if not readback_supported(receipt.provider, receipt.event_type, enabled):
        return "UNKNOWN", unavailable
    payload: dict[str, object] = {}
    if isinstance(receipt.payload_json, dict):
        payload = receipt.payload_json
    binding = await session.get(ExternalProjectBinding, receipt.binding_id)
    if binding is None:
        raise RuntimeError("binding not found")
    config = AoneConfig(
        binding.base_url,
        binding.client_key,
        crypto().decrypt(binding.credential_ref),
        binding.region_id,
    )
    comments = list_comments(_CLIENT, config, [_external_id(payload)])
    found = find_marked_comment(comments, marker_for_receipt(payload, receipt.operation_key))
    if found is None:
        return "UNKNOWN", COMMENT_NOT_FOUND
    await _link_outbound(session, receipt, payload, found)
    return "SUCCEEDED", None


def _external_id(payload: dict[str, object]) -> str:
    value = payload.get("externalWorkitemId")
    if isinstance(value, bool):
        raise RuntimeError("Aone comment readback is missing externalWorkitemId")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip() != "":
        return value
    raise RuntimeError("Aone comment readback is missing externalWorkitemId")


def _comment_id(payload: dict[str, object]) -> int | None:
    value = payload.get("commentId")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


async def _link_outbound(
    session: AsyncSession,
    receipt: IntegrationOutbox,
    payload: dict[str, object],
    found: ExternalComment,
) -> None:
    external_comment_id = found.external_id
    comment_id = _comment_id(payload)
    if external_comment_id is None or external_comment_id.strip() == "" or comment_id is None:
        return
    existing = await session.scalar(
        select(ExternalCommentLink)
        .where(
            ExternalCommentLink.tenant_id == receipt.tenant_id,
            ExternalCommentLink.workitem_comment_id == comment_id,
        )
        .limit(1)
    )
    if existing is not None:
        return
    source_status = "ACTIVE"
    if found.source_status is not None and found.source_status.strip() != "":
        source_status = found.source_status
    session.add(
        ExternalCommentLink(
            tenant_id=receipt.tenant_id,
            provider=receipt.provider,
            binding_id=receipt.binding_id,
            external_workitem_id=_external_id(payload),
            external_comment_id=external_comment_id,
            workitem_comment_id=comment_id,
            direction="OUTBOUND",
            source_updated_at=found.updated_at,
            source_status=source_status,
        )
    )
