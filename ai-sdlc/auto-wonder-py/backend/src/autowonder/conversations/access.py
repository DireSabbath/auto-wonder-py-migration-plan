"""平台管家会话的可见性。读开放给 Owner 和被分享人，写只认 Owner。"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.constants import PERMISSION_READ, PLATFORM_CHANNEL
from autowonder.conversations.models import AgentConversation
from autowonder.conversations.records import find_active_share, find_conversation
from autowonder.core.errors import BizError, ErrorCode


def is_owner(owner_user_id: int | None, user_id: int) -> bool:
    """没有归属人的会话谁都不是 Owner。"""
    return owner_user_id is not None and owner_user_id == user_id


def read_allowed(
    channel: str | None,
    deleted_at: datetime | None,
    owner_user_id: int | None,
    user_id: int,
    share_permission: str | None,
) -> bool:
    """非平台渠道、已删除、以及既不是 Owner 也没有 READ 分享时不可读。"""
    if channel != PLATFORM_CHANNEL or deleted_at is not None:
        return False
    if is_owner(owner_user_id, user_id):
        return True
    return share_permission == PERMISSION_READ


def write_error(
    channel: str | None,
    deleted_at: datetime | None,
    owner_user_id: int | None,
    user_id: int,
    share_permission: str | None,
) -> ErrorCode | None:
    """允许写入时返回 None。被分享人得到 Owner 专属码，其他人与不存在无法区分。"""
    if channel != PLATFORM_CHANNEL or deleted_at is not None:
        return ErrorCode.PLATFORM_CONVERSATION_NOT_FOUND_OR_NO_PERMISSION
    if is_owner(owner_user_id, user_id):
        return None
    if share_permission == PERMISSION_READ:
        return ErrorCode.PLATFORM_CONVERSATION_OWNER_ONLY
    return ErrorCode.PLATFORM_CONVERSATION_NOT_FOUND_OR_NO_PERMISSION


async def require_body_read(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
) -> AgentConversation:
    """Owner 或被分享人可以读正文。"""
    conversation = await _load_platform(session, tenant_id, conversation_id)
    if is_owner(conversation.owner_user_id, user_id) or await _has_read_share(
        session, tenant_id, conversation_id, user_id
    ):
        return conversation
    raise BizError(ErrorCode.PLATFORM_CONVERSATION_NOT_FOUND_OR_NO_PERMISSION)


async def require_body_write(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
) -> AgentConversation:
    """改名、归档、删除、分享、发消息和回答卡片只认 Owner。"""
    conversation = await _load_platform(session, tenant_id, conversation_id)
    if is_owner(conversation.owner_user_id, user_id):
        return conversation
    if await _has_read_share(session, tenant_id, conversation_id, user_id):
        raise BizError(ErrorCode.PLATFORM_CONVERSATION_OWNER_ONLY)
    raise BizError(ErrorCode.PLATFORM_CONVERSATION_NOT_FOUND_OR_NO_PERMISSION)


async def _load_platform(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> AgentConversation:
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if (
        conversation is None
        or conversation.channel != PLATFORM_CHANNEL
        or conversation.deleted_at is not None
    ):
        raise BizError(ErrorCode.PLATFORM_CONVERSATION_NOT_FOUND_OR_NO_PERMISSION)
    return conversation


async def _has_read_share(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
) -> bool:
    share = await find_active_share(session, tenant_id, conversation_id, user_id)
    return share is not None and share.permission == PERMISSION_READ
