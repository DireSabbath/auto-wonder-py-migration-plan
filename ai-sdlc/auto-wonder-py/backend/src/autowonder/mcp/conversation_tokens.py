"""会话 MCP 令牌。绑定 agent 与版本，会话结束后立即失效。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel
from autowonder.conversations.models import AgentConversation
from autowonder.core.errors import BizError, ErrorCode
from autowonder.mcp.principal import CredentialType, Principal
from autowonder.security.jwt import parse_conversation, sign_conversation

PREFIX = "awconversation_"
_PURPOSE = "conversation-mcp"
_TTL_SECONDS = 24 * 60 * 60
_PLATFORM_CHANNEL = "PLATFORM_ASSISTANT"


def issue_conversation_token(conversation: AgentConversation, user_id: int) -> str:
    """签给会话归属人。平台管家会话不能签给其他人。"""
    if (
        conversation.id is None
        or conversation.tenant_id is None
        or conversation.agent_id is None
        or conversation.agent_version_id is None
        or user_id <= 0
    ):
        raise ValueError("conversation MCP identity is incomplete")
    if conversation.channel == _PLATFORM_CHANNEL and conversation.owner_user_id != user_id:
        raise ValueError("conversation MCP identity does not match the owner")
    signed = sign_conversation(
        user_id,
        conversation.tenant_id,
        _PURPOSE,
        conversation.id,
        conversation.agent_id,
        conversation.agent_version_id,
        _TTL_SECONDS,
    )
    return PREFIX + signed


async def authenticate_conversation(session: AsyncSession, token: str | None) -> Principal:
    """会话关闭、换版本或主人不匹配时都是未授权。"""
    try:
        return await _principal(session, token)
    except Exception as error:
        raise _unauthorized() from error


async def _principal(session: AsyncSession, token: str | None) -> Principal:
    if token is None or not token.startswith(PREFIX):
        raise ValueError("invalid prefix")
    claims = parse_conversation(token[len(PREFIX) :])
    if claims.purpose != _PURPOSE:
        raise ValueError("invalid purpose")
    conversation = await session.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.tenant_id == claims.tenant_id,
            AgentConversation.id == claims.conversation_id,
        )
        .limit(1)
    )
    if conversation is None or conversation.status != "ACTIVE":
        raise ValueError("conversation is inactive")
    if (
        conversation.agent_id != claims.agent_id
        or conversation.agent_version_id != claims.agent_version_id
    ):
        raise ValueError("conversation agent binding changed")
    if conversation.channel == _PLATFORM_CHANNEL and conversation.owner_user_id != claims.user_id:
        raise ValueError("token owner does not match the conversation owner")
    return Principal(
        claims.tenant_id,
        claims.user_id,
        claims.conversation_id,
        _ceiling(conversation),
        CredentialType.CONVERSATION,
    )


def _ceiling(conversation: AgentConversation) -> WorkspaceAccessLevel:
    if conversation.channel == _PLATFORM_CHANNEL:
        return WorkspaceAccessLevel.READ_ONLY
    return WorkspaceAccessLevel.READ_WRITE


def _unauthorized() -> BizError:
    return BizError(ErrorCode.UNAUTHORIZED)
