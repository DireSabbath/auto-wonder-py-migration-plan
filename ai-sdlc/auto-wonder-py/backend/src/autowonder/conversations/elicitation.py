"""问答卡片的回答。accept 必须是 JSON 对象，decline 不带答案。"""

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.models import AgentConversation, AgentConversationElicitation
from autowonder.conversations.records import (
    find_conversation,
    find_elicitation,
    find_turn,
    insert_elicitation_if_absent,
    list_pending_by_turn,
    list_pending_elicitations,
    restore_pending_if_status,
    settle_if_pending,
)
from autowonder.conversations.routing import executor_online
from autowonder.conversations.schemas import ElicitationView
from autowonder.conversations.transport import send_elicitation_reply as deliver_elicitation_reply
from autowonder.core.errors import BizError, ErrorCode
from autowonder.ws.mailbox import next_server_event_seq, publish_conversation_event

logger = logging.getLogger(__name__)

_PENDING = "PENDING"
_ANSWERED = "ANSWERED"
_DECLINED = "DECLINED"
_CANCELED = "CANCELED"
_PROCESSING = "PROCESSING"


def normalize_elicitation_reply(action: str | None, answer_json: str | None) -> str | None:
    """decline 丢掉答案。accept 只接受 JSON 对象原文，不重排字段。"""
    if action == "decline":
        return None
    if action != "accept":
        raise BizError(ErrorCode.PARAM_INVALID, f"unsupported elicitation action: {action}")
    if answer_json is None or answer_json.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID, "elicitation answer is required")
    try:
        parsed = json.loads(answer_json)
    except json.JSONDecodeError as error:
        raise BizError(
            ErrorCode.PARAM_INVALID, "elicitation answer must be a JSON object"
        ) from error
    if not isinstance(parsed, dict):
        raise BizError(ErrorCode.PARAM_INVALID, "elicitation answer must be a JSON object")
    return answer_json


def elicitation_views(rows: list[AgentConversationElicitation]) -> list[ElicitationView]:
    """把挂起卡片回给会话详情。"""
    return [
        ElicitationView(
            request_id=row.request_id,
            turn_id=row.turn_id,
            mode=row.mode,
            message=row.message,
            requested_schema=row.schema_json,
            status=row.status,
            gmt_create=row.gmt_create,
        )
        for row in rows
    ]


async def list_pending(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> list[ElicitationView]:
    """详情页恢复未解决的卡片。"""
    rows = await list_pending_elicitations(session, tenant_id, conversation_id)
    return elicitation_views(rows)


async def reply(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    request_id: str,
    action: str | None,
    answer_json: str | None,
) -> None:
    """先抢挂起态，抢到才下发。投递失败时把仍属于这次的终态补回挂起。"""
    record = await find_elicitation(session, tenant_id, conversation_id, request_id)
    if record is None:
        raise BizError(
            ErrorCode.NOT_FOUND,
            "elicitation not found in this conversation: " + request_id,
        )
    if record.status != _PENDING:
        raise BizError(ErrorCode.CONFLICT, "elicitation already settled: " + record.status)
    normalized = normalize_elicitation_reply(action, answer_json)
    await _require_processing_turn(session, tenant_id, conversation_id, record.turn_id)
    conversation = await _require_online(session, tenant_id, conversation_id)
    status = _ANSWERED if action == "accept" else _DECLINED
    settled = await settle_if_pending(
        session, tenant_id, conversation_id, request_id, status, normalized
    )
    if settled != 1:
        raise BizError(
            ErrorCode.CONFLICT,
            "elicitation was settled concurrently: " + request_id,
        )
    try:
        await send_elicitation_reply(conversation, record.turn_id, request_id, action, normalized)
    except Exception as error:
        await restore_pending_if_status(session, tenant_id, conversation_id, request_id, status)
        logger.warning(
            "acp elicitation reply delivery failed conversationId=%s requestId=%s",
            conversation_id,
            request_id,
        )
        raise BizError(ErrorCode.SYSTEM_ERROR) from error
    await session.commit()


async def settle_pending_for_turn(
    session: AsyncSession,
    tenant_id: int,
    turn_id: int,
) -> list[AgentConversationElicitation]:
    """取消轮次前把该轮挂起卡片改成 CANCELED。抢不到的不重复动作。"""
    settled: list[AgentConversationElicitation] = []
    for record in await list_pending_by_turn(session, tenant_id, turn_id):
        won = await settle_if_pending(
            session,
            record.tenant_id,
            record.conversation_id,
            record.request_id,
            _CANCELED,
            None,
        )
        if won == 1:
            settled.append(record)
    return settled


async def send_elicitation_reply(
    conversation: AgentConversation,
    turn_id: int,
    request_id: str,
    action: str | None,
    answer_json: str | None,
) -> None:
    """把答案送回执行器。decline 与 cancel 不带答案。"""
    await deliver_elicitation_reply(conversation, turn_id, request_id, action, answer_json)


async def notify_canceled(
    session: AsyncSession,
    settled: list[AgentConversationElicitation],
) -> None:
    """提交之后才把取消帧和浏览器事件送出去。"""
    for record in settled:
        await notify_runtime_best_effort(session, record, "cancel", _CANCELED)
        await notify_browser_best_effort(record, "cancel")


async def notify_expired(
    session: AsyncSession,
    settled: list[AgentConversationElicitation],
) -> None:
    """过期卡片向执行器发 decline，并告诉浏览器卡片已经结束。"""
    for record in settled:
        await notify_runtime_best_effort(session, record, "decline", "EXPIRED")
        await notify_browser_best_effort(record, "decline")


async def notify_runtime_best_effort(
    session: AsyncSession,
    record: AgentConversationElicitation,
    action: str,
    status: str,
) -> None:
    """一张卡片投递失败不影响后面的卡片。"""
    conversation = await find_conversation(session, record.tenant_id, record.conversation_id)
    if conversation is None or conversation.executor_id is None:
        logger.info(
            "acp elicitation %s not delivered: conversation or executor gone "
            "conversationId=%s requestId=%s",
            status,
            record.conversation_id,
            record.request_id,
        )
        return
    try:
        await send_elicitation_reply(conversation, record.turn_id, record.request_id, action, None)
        logger.info(
            "acp elicitation %s delivered conversationId=%s turnId=%s requestId=%s",
            status,
            record.conversation_id,
            record.turn_id,
            record.request_id,
        )
    except Exception:
        logger.warning(
            "acp elicitation %s delivery failed conversationId=%s requestId=%s",
            status,
            record.conversation_id,
            record.request_id,
        )


async def notify_browser_best_effort(
    record: AgentConversationElicitation,
    action: str,
) -> None:
    """浏览器推送失败不回滚已经落下的终态。"""
    payload = json.dumps(
        {
            "type": "acp_elicitation_resolved",
            "data": {"requestId": record.request_id, "action": action},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    await publish_conversation_event(
        record.conversation_id,
        record.turn_id,
        next_server_event_seq(),
        "acp_elicitation_resolved",
        payload,
    )


def elicitation_terminal_status(action: object) -> str | None:
    """运行时 resolved 事件里的动作对应卡片终态。未知动作不落库。"""
    if action == "accept":
        return _ANSWERED
    if action == "decline":
        return _DECLINED
    if action == "cancel":
        return _CANCELED
    return None


async def on_runtime_event(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
    event_type: str,
    payload: str,
) -> None:
    """打开或结束问答卡片。畸形事件只记日志，不打断事件流。"""
    if not event_type.startswith("acp_elicitation"):
        return
    try:
        root = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning(
            "acp elicitation event payload unparsable conversationId=%s turnId=%s type=%s",
            conversation_id,
            turn_id,
            event_type,
        )
        return
    data = root.get("data") if isinstance(root, dict) else None
    request_id = data.get("requestId") if isinstance(data, dict) else None
    if not isinstance(request_id, str) or request_id.strip() == "":
        logger.warning(
            "acp elicitation event without requestId conversationId=%s turnId=%s type=%s",
            conversation_id,
            turn_id,
            event_type,
        )
        return
    if event_type == "acp_elicitation":
        mode = data.get("mode") if isinstance(data, dict) else None
        message = data.get("message") if isinstance(data, dict) else None
        schema = data.get("requestedSchema") if isinstance(data, dict) else None
        schema_json = None
        if schema is not None:
            schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        text = None
        if isinstance(message, str):
            text = message[:1024]
        chosen = "form"
        if isinstance(mode, str) and mode.strip() != "":
            chosen = mode
        await insert_elicitation_if_absent(
            session,
            AgentConversationElicitation(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                request_id=request_id,
                mode=chosen,
                message=text,
                schema_json=schema_json,
                status=_PENDING,
            ),
        )
        return
    if event_type == "acp_elicitation_resolved":
        action = data.get("action") if isinstance(data, dict) else None
        status = elicitation_terminal_status(action)
        if status is None:
            logger.warning(
                "acp elicitation resolved with unknown action conversationId=%s requestId=%s",
                conversation_id,
                request_id,
            )
            return
        await settle_if_pending(session, tenant_id, conversation_id, request_id, status, None)


async def _require_processing_turn(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
) -> None:
    turn = await find_turn(session, tenant_id, conversation_id, turn_id)
    if turn is None or turn.status != _PROCESSING:
        raise BizError(
            ErrorCode.CONFLICT,
            "elicitation turn is no longer processing: " + str(turn_id),
        )


async def _require_online(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> AgentConversation:
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if (
        conversation is None
        or conversation.executor_id is None
        or not await executor_online(conversation.executor_id)
    ):
        raise BizError(ErrorCode.SYSTEM_ERROR)
    return conversation
