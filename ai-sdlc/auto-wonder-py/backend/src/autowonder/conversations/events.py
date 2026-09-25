"""会话事件的分页读取。"""

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.constants import EVENT_PAGE_LIMIT, MAX_TURN_EVENTS
from autowonder.conversations.models import AgentConversationTurnEvent
from autowonder.conversations.records import list_events_after, list_events_by_turn
from autowonder.conversations.schemas import TurnEventView


def event_view(row: AgentConversationTurnEvent) -> TurnEventView:
    """事件行按 Java DO 的字段回给前端。"""
    return TurnEventView(
        id=row.id,
        tenant_id=row.tenant_id,
        conversation_id=row.conversation_id,
        turn_id=row.turn_id,
        dispatch_attempt=row.dispatch_attempt,
        event_seq=row.event_seq,
        chunk_index=row.chunk_index,
        chunk_count=row.chunk_count,
        event_type=row.event_type,
        payload_fragment=row.payload_fragment,
        gmt_create=row.gmt_create,
    )


async def list_after(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    after_id: int,
) -> list[TurnEventView]:
    """增量拉取，单次最多 200 条。"""
    rows = await list_events_after(session, tenant_id, conversation_id, after_id, EVENT_PAGE_LIMIT)
    return [event_view(row) for row in rows]


async def list_for_turn(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
) -> list[TurnEventView]:
    """按轮次回放，硬上限 5000 条。"""
    rows = await list_events_by_turn(session, tenant_id, conversation_id, turn_id, MAX_TURN_EVENTS)
    return [event_view(row) for row in rows]
