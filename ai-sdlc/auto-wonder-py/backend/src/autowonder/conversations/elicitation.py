"""问答卡片的回答。accept 必须是 JSON 对象，decline 不带答案。"""

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.models import AgentConversationElicitation
from autowonder.conversations.records import (
    find_conversation,
    find_elicitation,
    find_turn,
    list_pending_by_turn,
    list_pending_elicitations,
    restore_pending_if_status,
    settle_if_pending,
)
from autowonder.conversations.routing import executor_online
from autowonder.conversations.schemas import ElicitationView
from autowonder.core.errors import BizError, ErrorCode

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
    await _require_online(session, tenant_id, conversation_id)
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
        await send_elicitation_reply(conversation_id, record.turn_id, request_id, action)
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
    conversation_id: int,
    turn_id: int,
    request_id: str,
    action: str | None,
) -> None:
    """把答案送回执行器。WebSocket 传输尚未迁入。"""
    raise RuntimeError(
        "conversation runtime transport is not available: "
        + f"{conversation_id}:{turn_id}:{request_id}:{action}"
    )


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
) -> None:
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if (
        conversation is None
        or conversation.executor_id is None
        or not executor_online(conversation.executor_id)
    ):
        raise BizError(ErrorCode.SYSTEM_ERROR)
