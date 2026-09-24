"""入站轮次的提交、排队和取消。"""

import base64
import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.constants import (
    ACP_INTERACTION,
    CANCELED_FALLBACK_CONTENT,
    DIRECTION_IN,
    DIRECTION_OUT,
    LOCK_TIMEOUT_SECONDS,
    MAX_TURN_ERROR_LENGTH,
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_PROCESSING,
    STATUS_QUEUED,
    TURN_CANCEL,
)
from autowonder.conversations.elicitation import settle_pending_for_turn
from autowonder.conversations.models import AgentConversation, AgentConversationTurn
from autowonder.conversations.prompt import render_system_prompt
from autowonder.conversations.records import (
    find_agent,
    find_agent_version,
    find_by_external_message,
    find_by_key,
    find_conversation,
    find_next_queued_inbound,
    find_processing_inbound,
    find_turn,
    insert_conversation,
    insert_turn,
    record_dispatch_attempt,
    update_agent_version,
    update_executor,
    update_inbound_if_processing,
    update_status_and_last_turn,
    update_status_if_current,
)
from autowonder.conversations.routing import (
    ProtocolUnsupported,
    executor_online,
    protocol_features,
    protocol_supported,
    select_executor,
)
from autowonder.core.clock import now_local
from autowonder.core.context import current_request_id
from autowonder.core.errors import BizError, ErrorCode, IllegalArgumentError

logger = logging.getLogger(__name__)

STATUS_FAILED = "FAILED"


def platform_message_token(client_message_id: str | None, generated: str) -> str:
    """空白的客户端幂等键换成服务端生成的值，非空原样保留。"""
    if client_message_id is None or client_message_id.strip() == "":
        return generated
    return client_message_id


def platform_external_message_id(conversation_id: int, token: str) -> str:
    """幂等键带上会话 id，避免同工作空间的两个会话撞上同一个客户端 id。"""
    return f"web-platform:{conversation_id}:{token}"


def clarification_external_message_id(client_message_id: str | None) -> str:
    """澄清幂等键。没有客户端 id 时后缀是 Java 字符串拼接出的 null。"""
    suffix = "null" if client_message_id is None else client_message_id
    return "web-clarification:" + suffix


def logical_lock_name(
    tenant_id: int,
    channel: str,
    channel_conversation_id: str,
    agent_id: int,
) -> str:
    """按渠道会话键加锁，名字是 SHA-256 的 url-safe Base64。"""
    source = f"{tenant_id}\u001f{channel}\u001f{channel_conversation_id}\u001f{agent_id}"
    digest = hashlib.sha256(source.encode()).digest()
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return "agent-conv-key:" + encoded


def conversation_lock_name(tenant_id: int, conversation_id: int) -> str:
    """按已落库的会话主键加锁。"""
    return f"agent-conversation:{tenant_id}:{conversation_id}"


def dispatch_failure_summary(message: str | None, type_name: str) -> str:
    """投递失败写入轮次错误，最长 1024。空白消息用异常类型名。"""
    text_value = type_name
    if message is not None and message.strip() != "":
        text_value = message
    flattened = text_value.replace("\n", " ").replace("\r", " ").strip()
    summary = "conversation dispatch failed: " + flattened
    if len(summary) > MAX_TURN_ERROR_LENGTH:
        return summary[:MAX_TURN_ERROR_LENGTH]
    return summary


def canceled_reply_content(reply: str | None) -> str:
    """取消时没有部分正文就落一句「响应已终止」。"""
    if reply is not None and reply.strip() != "":
        return reply
    return CANCELED_FALLBACK_CONTENT


def resolve_processing(
    processing_status: str | None,
    processing_id: int | None,
    queued_status: str | None,
    queued_id: int | None,
) -> tuple[str | None, int | None]:
    """正在处理的入站优先；没有时用下一条排队。"""
    if processing_id is not None:
        return processing_status, processing_id
    if queued_id is not None:
        return queued_status, queued_id
    return None, None


def require_selected_executor(executor_id: int | None) -> int:
    """没有可选执行器时按运行时离线处理，响应是系统错误。"""
    if executor_id is None:
        raise BizError(ErrorCode.SYSTEM_ERROR)
    return executor_id


def require_cancelable(direction: str | None, status: str | None, turn_id: int) -> str:
    """只有处理中或排队中的入站轮次能取消。返回当前状态。"""
    if direction != DIRECTION_IN:
        raise BizError(ErrorCode.NOT_FOUND, "conversation turn not found: " + str(turn_id))
    if status != STATUS_PROCESSING and status != STATUS_QUEUED:
        raise BizError(ErrorCode.CONFLICT, "conversation turn is not cancelable: " + str(status))
    return status


@dataclass
class _Pending:
    conversation: AgentConversation
    turn_id: int
    content: str | None
    request_id: str | None
    system_prompt: str
    dispatch_attempt: int


async def submit_inbound(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    channel: str,
    channel_conversation_id: str,
    content: str | None,
    external_msg_id: str,
) -> None:
    """幂等插入入站轮次。已有轮次在处理时新消息排队，否则准备下发。"""
    pending: _Pending | None = None
    lock_name = logical_lock_name(tenant_id, channel, channel_conversation_id, agent_id)
    async with _Lock(session, lock_name):
        if await find_by_external_message(session, tenant_id, external_msg_id) is not None:
            return
        conversation = await find_by_key(
            session, tenant_id, channel, channel_conversation_id, agent_id
        )
        if conversation is None:
            pending = await _create_first(
                session,
                tenant_id,
                agent_id,
                channel,
                channel_conversation_id,
                content,
                external_msg_id,
            )
            await session.commit()
        else:
            async with _Lock(session, conversation_lock_name(tenant_id, conversation.id)):
                if await find_processing_inbound(session, tenant_id, conversation.id) is not None:
                    await _insert_inbound(
                        session,
                        tenant_id,
                        conversation.id,
                        content,
                        external_msg_id,
                        STATUS_QUEUED,
                    )
                    await session.commit()
                    return
                pending = await _prepare_existing(
                    session, tenant_id, conversation, content, external_msg_id
                )
                await session.commit()
    if pending is not None:
        await _send(session, pending)


async def request_turn_cancel(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
) -> None:
    """排队中的轮次直接终结。处理中的轮次要求执行器支持取消。"""
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if conversation is None:
        raise IllegalArgumentError("conversation not found")
    turn = await find_turn(session, tenant_id, conversation_id, turn_id)
    direction = None if turn is None else turn.direction
    status = None if turn is None else turn.status
    current = require_cancelable(direction, status, turn_id)
    if current == STATUS_QUEUED:
        async with _Lock(session, conversation_lock_name(tenant_id, conversation_id)):
            await _finalize_cancel(session, tenant_id, conversation, turn_id, STATUS_QUEUED)
            await session.commit()
        promoted = await _promote(session, conversation)
        if promoted is not None:
            await _send(session, promoted)
        return
    online = executor_online(conversation.executor_id)
    features = protocol_features(conversation.executor_id)
    if conversation.executor_id is None or not protocol_supported(online, features, TURN_CANCEL):
        raise BizError(ErrorCode.CONFLICT, "runtime does not support conversation turn cancel")
    try:
        await settle_pending_for_turn(session, tenant_id, turn_id)
    except Exception:
        logger.warning(
            "conversation pending elicitation cancel failed conversationId=%s turnId=%s",
            conversation_id,
            turn_id,
        )
    try:
        await send_cancel(conversation_id, turn_id)
    except Exception as error:
        raise BizError(ErrorCode.SYSTEM_ERROR) from error
    await session.commit()


async def send_cancel(conversation_id: int, turn_id: int) -> None:
    """通知执行器停止生成。WebSocket 传输尚未迁入。"""
    raise RuntimeError(
        "conversation runtime transport is not available: " + f"{conversation_id}:{turn_id}"
    )


async def deliver_turn(pending: _Pending) -> None:
    """把轮次交给执行器。WebSocket 传输尚未迁入。"""
    raise RuntimeError(
        "conversation runtime transport is not available: "
        + f"{pending.conversation.id}:{pending.turn_id}"
    )


async def _create_first(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    channel: str,
    channel_conversation_id: str,
    content: str | None,
    external_msg_id: str,
) -> _Pending | None:
    version_id = await _online_version_id(session, agent_id)
    try:
        executor_id = await select_executor(session, tenant_id, agent_id, version_id, None)
    except ProtocolUnsupported as error:
        raise BizError(ErrorCode.SYSTEM_ERROR) from error
    bound = require_selected_executor(executor_id)
    conversation = AgentConversation(
        tenant_id=tenant_id,
        agent_id=agent_id,
        agent_version_id=version_id,
        channel=channel,
        channel_conversation_id=channel_conversation_id,
        executor_id=bound,
        status=STATUS_ACTIVE,
        last_turn_at=now_local(),
    )
    await insert_conversation(session, conversation)
    return await _prepare_inserted(
        session, tenant_id, conversation, content, external_msg_id, version_id
    )


async def _prepare_existing(
    session: AsyncSession,
    tenant_id: int,
    conversation: AgentConversation,
    content: str | None,
    external_msg_id: str,
) -> _Pending | None:
    version_id = await _online_version_id(session, conversation.agent_id)
    try:
        executor_id = await select_executor(
            session, tenant_id, conversation.agent_id, version_id, conversation.executor_id
        )
    except ProtocolUnsupported as error:
        raise BizError(ErrorCode.SYSTEM_ERROR) from error
    bound = require_selected_executor(executor_id)
    await _bind_executor(session, tenant_id, conversation, bound)
    return await _prepare_inserted(
        session, tenant_id, conversation, content, external_msg_id, version_id
    )


async def _prepare_inserted(
    session: AsyncSession,
    tenant_id: int,
    conversation: AgentConversation,
    content: str | None,
    external_msg_id: str,
    version_id: int,
) -> _Pending | None:
    turn = await _insert_inbound(
        session, tenant_id, conversation.id, content, external_msg_id, STATUS_PROCESSING
    )
    if await record_dispatch_attempt(session, tenant_id, conversation.id, turn.id) != 1:
        return None
    prompt = await _refresh_prompt(session, tenant_id, conversation, version_id)
    return _Pending(
        conversation,
        turn.id,
        content,
        current_request_id(),
        prompt,
        1,
    )


async def _promote(session: AsyncSession, conversation: AgentConversation) -> _Pending | None:
    if await find_processing_inbound(session, conversation.tenant_id, conversation.id) is not None:
        return None
    queued = await find_next_queued_inbound(session, conversation.tenant_id, conversation.id)
    if queued is None:
        return None
    try:
        version_id = await _online_version_id(session, conversation.agent_id)
        executor_id = await select_executor(
            session,
            conversation.tenant_id,
            conversation.agent_id,
            version_id,
            conversation.executor_id,
        )
    except ProtocolUnsupported as error:
        await update_status_if_current(
            session,
            conversation.tenant_id,
            queued.id,
            STATUS_QUEUED,
            STATUS_FAILED,
            "conversation executor compatibility failed: " + str(error),
        )
        await session.commit()
        return None
    if executor_id is None:
        return None
    moved = await update_status_if_current(
        session, conversation.tenant_id, queued.id, STATUS_QUEUED, STATUS_PROCESSING, None
    )
    if moved != 1:
        return None
    recorded = await record_dispatch_attempt(
        session, conversation.tenant_id, conversation.id, queued.id
    )
    if recorded != 1:
        return None
    await _bind_executor(session, conversation.tenant_id, conversation, executor_id)
    prompt = await _refresh_prompt(session, conversation.tenant_id, conversation, version_id)
    await session.commit()
    return _Pending(conversation, queued.id, queued.content, queued.request_id, prompt, 1)


async def _finalize_cancel(
    session: AsyncSession,
    tenant_id: int,
    conversation: AgentConversation,
    turn_id: int,
    from_status: str,
) -> None:
    if from_status == STATUS_PROCESSING:
        finalized = await update_inbound_if_processing(
            session, tenant_id, conversation.id, turn_id, STATUS_CANCELED, None
        )
    else:
        finalized = await update_status_if_current(
            session, tenant_id, turn_id, from_status, STATUS_CANCELED, None
        )
    if finalized != 1:
        return
    await insert_turn(
        session,
        AgentConversationTurn(
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            direction=DIRECTION_OUT,
            content=canceled_reply_content(None),
            request_id=current_request_id(),
            status=STATUS_CANCELED,
        ),
    )
    await update_status_and_last_turn(
        session, tenant_id, conversation.id, STATUS_ACTIVE, now_local()
    )


async def _send(session: AsyncSession, pending: _Pending) -> None:
    current: _Pending | None = pending
    while current is not None:
        try:
            await deliver_turn(current)
        except Exception as error:
            current = await _mark_failed(session, current, error)
            continue
        current = None


async def _mark_failed(
    session: AsyncSession,
    pending: _Pending,
    error: Exception,
) -> _Pending | None:
    summary = dispatch_failure_summary(str(error), type(error).__name__)
    finalized = await update_inbound_if_processing(
        session,
        pending.conversation.tenant_id,
        pending.conversation.id,
        pending.turn_id,
        STATUS_FAILED,
        summary,
    )
    if finalized == 1:
        await insert_turn(
            session,
            AgentConversationTurn(
                tenant_id=pending.conversation.tenant_id,
                conversation_id=pending.conversation.id,
                direction=DIRECTION_OUT,
                content="回复失败：" + summary,
                request_id=pending.request_id,
                status=STATUS_FAILED,
                error=summary,
            ),
        )
        await update_status_and_last_turn(
            session,
            pending.conversation.tenant_id,
            pending.conversation.id,
            STATUS_ACTIVE,
            now_local(),
        )
    await session.commit()
    logger.error(
        "conversation runtime dispatch failed conversationId=%s turnId=%s",
        pending.conversation.id,
        pending.turn_id,
    )
    if finalized != 1:
        return None
    return await _promote(session, pending.conversation)


async def _online_version_id(session: AsyncSession, agent_id: int) -> int:
    agent = await find_agent(session, agent_id)
    if agent is None or agent.is_deleted == 1 or agent.online_version_id is None:
        raise BizError(ErrorCode.SYSTEM_ERROR)
    return agent.online_version_id


async def _bind_executor(
    session: AsyncSession,
    tenant_id: int,
    conversation: AgentConversation,
    executor_id: int,
) -> None:
    if executor_id == conversation.executor_id:
        return
    conversation.executor_id = executor_id
    await update_executor(session, tenant_id, conversation.id, executor_id)


async def _refresh_prompt(
    session: AsyncSession,
    tenant_id: int,
    conversation: AgentConversation,
    version_id: int,
) -> str:
    version = await find_agent_version(session, version_id)
    if version is None:
        raise BizError(ErrorCode.SYSTEM_ERROR)
    if version.id != conversation.agent_version_id:
        updated = await update_agent_version(session, tenant_id, conversation.id, version.id)
        if updated != 1:
            raise BizError(ErrorCode.SYSTEM_ERROR)
        conversation.agent_version_id = version.id
    features = protocol_features(conversation.executor_id)
    acp = protocol_supported(executor_online(conversation.executor_id), features, ACP_INTERACTION)
    return render_system_prompt(
        version.role_name,
        version.role_code,
        version.business_background,
        version.responsibilities,
        version.identity_json,
        conversation.channel,
        acp,
    )


async def _insert_inbound(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    content: str | None,
    external_msg_id: str,
    status: str,
) -> AgentConversationTurn:
    turn = AgentConversationTurn(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        direction=DIRECTION_IN,
        content=content,
        external_msg_id=external_msg_id,
        request_id=current_request_id(),
        status=status,
    )
    await insert_turn(session, turn)
    return turn


class _Lock:
    """会话锁用 MySQL GET_LOCK，离开时释放。"""

    def __init__(self, session: AsyncSession, name: str) -> None:
        self._session = session
        self._name = name

    async def __aenter__(self) -> "_Lock":
        acquired = await self._session.scalar(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": self._name, "timeout": LOCK_TIMEOUT_SECONDS},
        )
        if acquired != 1:
            raise BizError(ErrorCode.SYSTEM_ERROR)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        try:
            released = await self._session.scalar(
                text("SELECT RELEASE_LOCK(:name)"),
                {"name": self._name},
            )
        except Exception:
            logger.warning("conversation lock release failed lockName=%s", self._name)
            return
        if released != 1:
            logger.warning(
                "conversation lock release returned %s lockName=%s",
                released,
                self._name,
            )
