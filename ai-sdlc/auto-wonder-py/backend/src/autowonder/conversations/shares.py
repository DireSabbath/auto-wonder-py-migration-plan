"""平台会话的只读分享。被分享人不能续聊。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.models import AgentConversation, ConversationShare
from autowonder.conversations.records import list_active_shares, revoke_share, upsert_read_share
from autowonder.conversations.schemas import ShareView
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.workspaces.service import current_membership


def share_target_invalid(grantee_user_id: int | None, owner_user_id: int | None) -> bool:
    """空、非正数，或分享给 Owner 本人，都不是合法对象。"""
    if grantee_user_id is None or grantee_user_id <= 0:
        return True
    return grantee_user_id == owner_user_id


def share_views(rows: list[ConversationShare]) -> list[ShareView]:
    """分享名单。"""
    return [
        ShareView(
            grantee_user_id=row.grantee_user_id,
            permission=row.permission,
            gmt_create=row.gmt_create,
        )
        for row in rows
    ]


async def list_shares(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> list[ShareView]:
    """列出仍有效的分享。"""
    return share_views(await list_active_shares(session, tenant_id, conversation_id))


async def grant_read_share(
    session: AsyncSession,
    tenant_id: int,
    conversation: AgentConversation,
    grantee_user_id: int | None,
) -> list[ShareView]:
    """分享给同工作空间的在职成员，并返回最新名单。"""
    if grantee_user_id is None or share_target_invalid(
        grantee_user_id, conversation.owner_user_id
    ):
        raise BizError(ErrorCode.PLATFORM_CONVERSATION_SHARE_INVALID)
    await current_membership(session, tenant_id, grantee_user_id)
    await upsert_read_share(
        session,
        tenant_id,
        conversation.id,
        grantee_user_id,
        conversation.owner_user_id,
    )
    await session.commit()
    return await list_shares(session, tenant_id, conversation.id)


async def revoke_read_share(
    session: AsyncSession,
    tenant_id: int,
    conversation: AgentConversation,
    grantee_user_id: int,
) -> list[ShareView]:
    """取消一名被分享人的只读权限，并返回最新名单。"""
    await revoke_share(
        session,
        tenant_id,
        conversation.id,
        grantee_user_id,
        conversation.owner_user_id,
        now_local(),
    )
    await session.commit()
    return await list_shares(session, tenant_id, conversation.id)
