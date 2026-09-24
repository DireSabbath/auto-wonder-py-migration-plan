"""执行器回报的会话确认、事件和斜杠命令。"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.constants import (
    CANCELED_FALLBACK_CONTENT,
    DIRECTION_IN,
    DIRECTION_OUT,
    SNAPSHOT_KEY_PREFIX,
    SNAPSHOT_TTL_SEC,
    STATUS_ACTIVE,
    STATUS_CANCELED,
    STATUS_PROCESSING,
)
from autowonder.conversations.elicitation import on_runtime_event
from autowonder.conversations.models import (
    AgentConversationTurn,
    AgentConversationTurnEvent,
)
from autowonder.conversations.records import (
    find_conversation,
    find_processing_inbound,
    insert_event_chunk_if_absent,
    insert_turn,
    update_cli_session_ref,
    update_inbound_if_processing,
    update_status_and_last_turn,
)
from autowonder.core.clock import now_local
from autowonder.core.redis import redis_client
from autowonder.ws.mailbox import next_server_event_seq, publish_conversation_event

logger = logging.getLogger(__name__)


def outbound_reply_content(reply_markdown: str | None, error: str | None) -> str:
    """出站正文优先用回复；没有回复时用失败说明，再没有就落一句占位。"""
    if reply_markdown is not None and reply_markdown.strip() != "":
        return reply_markdown
    if error is not None and error.strip() != "":
        return "回复失败：" + error
    return "（数字人未返回内容）"


def ack_reply_content(status: str | None, reply_markdown: str | None, error: str | None) -> str:
    """取消用终止文案，其余状态走普通出站正文。"""
    if status is not None and status.upper() == STATUS_CANCELED:
        if reply_markdown is not None and reply_markdown.strip() != "":
            return reply_markdown
        return CANCELED_FALLBACK_CONTENT
    return outbound_reply_content(reply_markdown, error)


async def acknowledge_turn(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    conversation_id: int,
    turn_id: int,
    status: str | None,
    error: str | None,
    reply_markdown: str | None,
    cli_session_id: str | None,
) -> None:
    """粘性执行器确认处理中的入站轮次，并补一条出站轮次。"""
    logger.info(
        "inbound CONVERSATION_TURN_ACK conversationId=%s status=%s executorId=%s",
        conversation_id,
        status,
        executor_id,
    )
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if conversation is None:
        return
    if conversation.executor_id is not None and executor_id != conversation.executor_id:
        logger.warning(
            "conversation ack rejected: executor %s != owner %s conversationId=%s",
            executor_id,
            conversation.executor_id,
            conversation_id,
        )
        return
    inbound = await session.get(AgentConversationTurn, turn_id)
    if inbound is not None and inbound.tenant_id != tenant_id:
        inbound = None
    if not _active_inbound(inbound, conversation_id):
        logger.warning(
            "conversation ack ignored: invalid inbound turn conversationId=%s turnId=%s",
            conversation_id,
            turn_id,
        )
        return
    if status is None:
        return
    finalized = await update_inbound_if_processing(
        session,
        tenant_id,
        conversation_id,
        turn_id,
        status,
        error,
    )
    if finalized != 1:
        logger.warning(
            "conversation ack ignored: turn not active IN PROCESSING conversationId=%s turnId=%s",
            conversation_id,
            turn_id,
        )
        return
    if cli_session_id is not None and cli_session_id != "":
        await update_cli_session_ref(session, tenant_id, conversation_id, cli_session_id)
    await insert_turn(
        session,
        AgentConversationTurn(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            direction=DIRECTION_OUT,
            content=ack_reply_content(status, reply_markdown, error),
            status=status,
            error=error,
        ),
    )
    await update_status_and_last_turn(
        session,
        tenant_id,
        conversation_id,
        STATUS_ACTIVE,
        now_local(),
    )
    await session.commit()
    payload = '{"type":"status","status":"' + status + '"}'
    await publish_conversation_event(
        conversation_id,
        turn_id,
        next_server_event_seq(),
        "status",
        payload,
    )


async def persist_turn_event(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    conversation_id: int,
    turn_id: int,
    dispatch_attempt: int,
    event_seq: int,
    chunk_index: int,
    chunk_count: int,
    event_type: str | None,
    payload_fragment: str | None,
) -> None:
    """校验粘性执行器和当前轮次后落下事件分片。拼齐后推给浏览器。"""
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if conversation is None or conversation.executor_id != executor_id:
        logger.warning(
            "conversation event rejected: executor mismatch tenantId=%s conversationId=%s "
            "executorId=%s",
            tenant_id,
            conversation_id,
            executor_id,
        )
        return
    turn = await find_processing_inbound(session, tenant_id, conversation_id)
    if turn is None or turn.id != turn_id:
        logger.warning(
            "conversation event rejected: no active turn tenantId=%s conversationId=%s turnId=%s",
            tenant_id,
            conversation_id,
            turn_id,
        )
        return
    if event_type is None or payload_fragment is None:
        return
    await insert_event_chunk_if_absent(
        session,
        AgentConversationTurnEvent(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            dispatch_attempt=dispatch_attempt,
            event_seq=event_seq,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            event_type=event_type,
            payload_fragment=payload_fragment,
        ),
    )
    assembled = payload_fragment
    if chunk_count > 1:
        joined = await _assembled_chunks(
            session,
            tenant_id,
            turn_id,
            dispatch_attempt,
            event_seq,
            chunk_count,
        )
        if joined is None:
            await session.commit()
            return
        assembled = joined
    if event_type == "status":
        await _remember_cli_session(session, tenant_id, conversation_id, assembled)
    try:
        await on_runtime_event(session, tenant_id, conversation_id, turn_id, event_type, assembled)
    except Exception:
        logger.warning(
            "acp elicitation event handling failed conversationId=%s turnId=%s type=%s",
            conversation_id,
            turn_id,
            event_type,
        )
    await session.commit()
    await publish_conversation_event(
        conversation_id,
        turn_id,
        event_seq,
        event_type,
        assembled,
    )


async def store_commands_result(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    conversation_id: int,
    status: str | None,
    commands_json: str | None,
    error: str | None,
) -> None:
    """归属正确且状态为 OK 时，把命令快照写入 Redis 并推给浏览器。"""
    if conversation_id <= 0:
        logger.warning(
            "commands result rejected: malformed conversationId=%s executorId=%s",
            conversation_id,
            executor_id,
        )
        return
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if conversation is None or conversation.executor_id != executor_id:
        logger.warning(
            "commands result rejected: executor mismatch tenantId=%s conversationId=%s "
            "executorId=%s",
            tenant_id,
            conversation_id,
            executor_id,
        )
        return
    if status != "OK" or commands_json is None or commands_json.strip() == "":
        logger.info(
            "commands probe not ok conversationId=%s status=%s error=%s",
            conversation_id,
            status,
            error,
        )
        return
    try:
        await redis_client().set(
            SNAPSHOT_KEY_PREFIX + str(conversation_id),
            commands_json,
            ex=SNAPSHOT_TTL_SEC,
        )
    except Exception:
        logger.warning("commands snapshot write degraded conversationId=%s", conversation_id)
    payload = '{"type":"acp_commands","data":' + commands_json + "}"
    await publish_conversation_event(
        conversation_id,
        0,
        next_server_event_seq(),
        "acp_commands",
        payload,
    )


def _active_inbound(turn: AgentConversationTurn | None, conversation_id: int) -> bool:
    return (
        turn is not None
        and turn.conversation_id == conversation_id
        and turn.direction == DIRECTION_IN
        and turn.status == STATUS_PROCESSING
    )


async def _assembled_chunks(
    session: AsyncSession,
    tenant_id: int,
    turn_id: int,
    dispatch_attempt: int,
    event_seq: int,
    expected: int,
) -> str | None:
    rows = list(
        await session.scalars(
            select(AgentConversationTurnEvent)
            .where(
                AgentConversationTurnEvent.tenant_id == tenant_id,
                AgentConversationTurnEvent.turn_id == turn_id,
                AgentConversationTurnEvent.dispatch_attempt == dispatch_attempt,
                AgentConversationTurnEvent.event_seq == event_seq,
            )
            .order_by(AgentConversationTurnEvent.chunk_index.asc())
        )
    )
    if len(rows) != expected:
        return None
    assembled = "".join(row.payload_fragment for row in rows)
    try:
        json.loads(assembled)
    except json.JSONDecodeError:
        logger.warning(
            "conversation event chunk reassembly produced invalid JSON turnId=%s eventSeq=%s",
            turn_id,
            event_seq,
        )
        return None
    return assembled


async def _remember_cli_session(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    payload: str,
) -> None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    session_id = parsed.get("sessionId")
    if isinstance(session_id, str) and session_id.strip() != "":
        await update_cli_session_ref(session, tenant_id, conversation_id, session_id)
