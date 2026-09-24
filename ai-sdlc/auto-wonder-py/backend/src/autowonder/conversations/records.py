"""会话表的读写。条件与 Java Mapper 一致。"""

from datetime import datetime
from typing import cast

from sqlalchemy import func, null, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent, AgentEnvironmentVariableRef, AgentVersion
from autowonder.conversations.constants import (
    PERMISSION_READ,
    PLATFORM_CHANNEL,
    STATUS_PROCESSING,
    STATUS_QUEUED,
)
from autowonder.conversations.models import (
    AgentConversation,
    AgentConversationElicitation,
    AgentConversationTurn,
    AgentConversationTurnEvent,
    ConversationShare,
)
from autowonder.db.rows import rowcount


async def find_conversation(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> AgentConversation | None:
    """按工作空间和主键取会话。"""
    return await session.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.tenant_id == tenant_id,
            AgentConversation.id == conversation_id,
        )
        .limit(1)
    )


async def find_by_key(
    session: AsyncSession,
    tenant_id: int,
    channel: str,
    channel_conversation_id: str,
    agent_id: int,
) -> AgentConversation | None:
    """按渠道会话键取已有线程。"""
    return await session.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.tenant_id == tenant_id,
            AgentConversation.channel == channel,
            AgentConversation.channel_conversation_id == channel_conversation_id,
            AgentConversation.agent_id == agent_id,
        )
        .limit(1)
    )


async def list_platform_by_owner(
    session: AsyncSession,
    tenant_id: int,
    owner_user_id: int,
    archived: bool | None,
    keyword: str | None,
    limit: int,
    offset: int,
) -> list[AgentConversation]:
    """只列出调用者自己的、未删除的平台管家会话。"""
    stmt = select(AgentConversation).where(
        AgentConversation.tenant_id == tenant_id,
        AgentConversation.channel == PLATFORM_CHANNEL,
        AgentConversation.owner_user_id == owner_user_id,
        AgentConversation.deleted_at.is_(None),
    )
    if archived is True:
        stmt = stmt.where(AgentConversation.archived_at.is_not(None))
    elif archived is False:
        stmt = stmt.where(AgentConversation.archived_at.is_(None))
    if keyword is not None:
        pattern = f"%{keyword}%"
        stmt = stmt.where(
            AgentConversation.title.like(pattern)
            | AgentConversation.channel_conversation_id.like(pattern)
        )
    stmt = stmt.order_by(AgentConversation.last_turn_at.desc(), AgentConversation.id.desc())
    stmt = stmt.limit(limit).offset(offset)
    return list((await session.scalars(stmt)).all())


async def list_by_biz_ref(
    session: AsyncSession,
    tenant_id: int,
    channel: str,
    biz_ref_type: str,
    biz_ref_id: int,
    agent_id: int,
) -> list[AgentConversation]:
    """按工单和数字人列出澄清会话，新的在前。"""
    stmt = (
        select(AgentConversation)
        .where(
            AgentConversation.tenant_id == tenant_id,
            AgentConversation.channel == channel,
            AgentConversation.biz_ref_type == biz_ref_type,
            AgentConversation.biz_ref_id == biz_ref_id,
            AgentConversation.agent_id == agent_id,
        )
        .order_by(AgentConversation.gmt_create.desc(), AgentConversation.id.desc())
    )
    return list((await session.scalars(stmt)).all())


async def insert_conversation(session: AsyncSession, conversation: AgentConversation) -> None:
    """插入会话并取回主键。"""
    session.add(conversation)
    await session.flush()


async def update_platform_metadata(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    owner_user_id: int | None,
    title: str | None,
    title_source: str | None,
    archived_at: datetime | None,
) -> None:
    """改标题或归档时间。标题为 None 时不改标题列。"""
    values: dict[str, object] = {
        "archived_at": archived_at,
        "version": AgentConversation.version + 1,
    }
    if title is not None:
        values["title"] = title
        values["title_source"] = title_source
    await session.execute(
        update(AgentConversation)
        .where(
            AgentConversation.tenant_id == tenant_id,
            AgentConversation.id == conversation_id,
            AgentConversation.channel == PLATFORM_CHANNEL,
            AgentConversation.owner_user_id == owner_user_id,
            AgentConversation.deleted_at.is_(None),
        )
        .values(**values)
    )


async def mark_platform_deleted(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    owner_user_id: int | None,
    deleted_at: datetime,
) -> None:
    """软删除平台管家会话。"""
    await session.execute(
        update(AgentConversation)
        .where(
            AgentConversation.tenant_id == tenant_id,
            AgentConversation.id == conversation_id,
            AgentConversation.channel == PLATFORM_CHANNEL,
            AgentConversation.owner_user_id == owner_user_id,
            AgentConversation.deleted_at.is_(None),
        )
        .values(deleted_at=deleted_at, version=AgentConversation.version + 1)
    )


async def update_executor(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    executor_id: int,
) -> None:
    """换粘性执行器。"""
    await session.execute(
        update(AgentConversation)
        .where(AgentConversation.tenant_id == tenant_id, AgentConversation.id == conversation_id)
        .values(executor_id=executor_id, version=AgentConversation.version + 1)
    )


async def update_agent_version(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    agent_version_id: int,
) -> int:
    """刷新会话使用的在线版本，返回变更行数。"""
    result = await session.execute(
        update(AgentConversation)
        .where(AgentConversation.tenant_id == tenant_id, AgentConversation.id == conversation_id)
        .values(agent_version_id=agent_version_id, version=AgentConversation.version + 1)
    )
    return rowcount(result)


async def update_status_and_last_turn(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    status: str,
    last_turn_at: datetime,
) -> None:
    """轮次终结后把会话标回活跃并刷新最后活动时间。"""
    await session.execute(
        update(AgentConversation)
        .where(AgentConversation.tenant_id == tenant_id, AgentConversation.id == conversation_id)
        .values(status=status, last_turn_at=last_turn_at, version=AgentConversation.version + 1)
    )


async def find_agent(session: AsyncSession, agent_id: int) -> Agent | None:
    """按主键取数字人，包含已删除行，供入口自己判断。"""
    return await session.scalar(select(Agent).where(Agent.id == agent_id).limit(1))


async def find_agent_version(session: AsyncSession, version_id: int) -> AgentVersion | None:
    """取未删除的数字人版本。"""
    return await session.scalar(
        select(AgentVersion)
        .where(AgentVersion.id == version_id, AgentVersion.is_deleted == 0)
        .limit(1)
    )


async def count_environment_refs(
    session: AsyncSession,
    tenant_id: int,
    agent_version_id: int,
) -> int:
    """在线版本绑定的环境变量数量。有绑定时选执行器要声明对应协议。"""
    result = await session.scalar(
        select(func.count())
        .select_from(AgentEnvironmentVariableRef)
        .where(
            AgentEnvironmentVariableRef.tenant_id == tenant_id,
            AgentEnvironmentVariableRef.agent_version_id == agent_version_id,
        )
    )
    return cast(int, result)


async def list_turns(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> list[AgentConversationTurn]:
    """按主键顺序列出一轮会话的全部消息。"""
    stmt = (
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.tenant_id == tenant_id,
            AgentConversationTurn.conversation_id == conversation_id,
        )
        .order_by(AgentConversationTurn.id.asc())
    )
    return list((await session.scalars(stmt)).all())


async def find_turn(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
) -> AgentConversationTurn | None:
    """按会话和轮次主键取消息。"""
    return await session.scalar(
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.tenant_id == tenant_id,
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.id == turn_id,
        )
        .limit(1)
    )


async def find_by_external_message(
    session: AsyncSession,
    tenant_id: int,
    external_msg_id: str,
) -> AgentConversationTurn | None:
    """入站幂等键。同一工作空间里已存在则不再插入。"""
    return await session.scalar(
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.tenant_id == tenant_id,
            AgentConversationTurn.external_msg_id == external_msg_id,
        )
        .limit(1)
    )


async def find_processing_inbound(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> AgentConversationTurn | None:
    """当前正在处理的入站轮次。"""
    return await session.scalar(
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.tenant_id == tenant_id,
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.direction == "IN",
            AgentConversationTurn.status == STATUS_PROCESSING,
        )
        .order_by(AgentConversationTurn.id.asc())
        .limit(1)
    )


async def find_next_queued_inbound(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> AgentConversationTurn | None:
    """下一条还没下发的入站轮次。"""
    return await session.scalar(
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.tenant_id == tenant_id,
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.direction == "IN",
            AgentConversationTurn.status == STATUS_QUEUED,
        )
        .order_by(AgentConversationTurn.gmt_create.asc(), AgentConversationTurn.id.asc())
        .limit(1)
    )


async def insert_turn(session: AsyncSession, turn: AgentConversationTurn) -> None:
    """插入轮次并取回主键。"""
    session.add(turn)
    await session.flush()


async def update_inbound_if_processing(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
    status: str,
    error: str | None,
) -> int:
    """只把仍在处理的入站轮次改成终态。"""
    result = await session.execute(
        update(AgentConversationTurn)
        .where(
            AgentConversationTurn.tenant_id == tenant_id,
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.id == turn_id,
            AgentConversationTurn.direction == "IN",
            AgentConversationTurn.status == STATUS_PROCESSING,
        )
        .values(status=status, error=error)
    )
    return rowcount(result)


async def update_status_if_current(
    session: AsyncSession,
    tenant_id: int,
    turn_id: int,
    from_status: str,
    to_status: str,
    error: str | None,
) -> int:
    """按当前状态做一次转移，返回变更行数。"""
    result = await session.execute(
        update(AgentConversationTurn)
        .where(
            AgentConversationTurn.tenant_id == tenant_id,
            AgentConversationTurn.id == turn_id,
            AgentConversationTurn.status == from_status,
        )
        .values(status=to_status, error=error)
    )
    return rowcount(result)


async def record_dispatch_attempt(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
) -> int:
    """处理中的入站轮次记一次投递。"""
    result = await session.execute(
        update(AgentConversationTurn)
        .where(
            AgentConversationTurn.tenant_id == tenant_id,
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.id == turn_id,
            AgentConversationTurn.direction == "IN",
            AgentConversationTurn.status == STATUS_PROCESSING,
        )
        .values(
            last_dispatch_at=func.now(),
            dispatch_attempt=AgentConversationTurn.dispatch_attempt + 1,
        )
    )
    return rowcount(result)


async def list_events_after(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    after_id: int,
    limit: int,
) -> list[AgentConversationTurnEvent]:
    """取主键大于 afterId 的事件，最多 limit 条。"""
    stmt = (
        select(AgentConversationTurnEvent)
        .where(
            AgentConversationTurnEvent.tenant_id == tenant_id,
            AgentConversationTurnEvent.conversation_id == conversation_id,
            AgentConversationTurnEvent.id > after_id,
        )
        .order_by(AgentConversationTurnEvent.id.asc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def list_events_by_turn(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    turn_id: int,
    limit: int,
) -> list[AgentConversationTurnEvent]:
    """按事件序号和分片序号取一轮的事件。"""
    stmt = (
        select(AgentConversationTurnEvent)
        .where(
            AgentConversationTurnEvent.tenant_id == tenant_id,
            AgentConversationTurnEvent.conversation_id == conversation_id,
            AgentConversationTurnEvent.turn_id == turn_id,
        )
        .order_by(
            AgentConversationTurnEvent.event_seq.asc(),
            AgentConversationTurnEvent.chunk_index.asc(),
        )
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def find_active_share(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    grantee_user_id: int,
) -> ConversationShare | None:
    """未撤销的分享。"""
    return await session.scalar(
        select(ConversationShare)
        .where(
            ConversationShare.tenant_id == tenant_id,
            ConversationShare.conversation_id == conversation_id,
            ConversationShare.grantee_user_id == grantee_user_id,
            ConversationShare.revoked_at.is_(None),
        )
        .limit(1)
    )


async def list_active_shares(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> list[ConversationShare]:
    """仍有效的分享，按被分享人排序。"""
    stmt = (
        select(ConversationShare)
        .where(
            ConversationShare.tenant_id == tenant_id,
            ConversationShare.conversation_id == conversation_id,
            ConversationShare.revoked_at.is_(None),
        )
        .order_by(ConversationShare.grantee_user_id.asc(), ConversationShare.id.asc())
    )
    return list((await session.scalars(stmt)).all())


async def upsert_read_share(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    grantee_user_id: int,
    created_by: int | None,
) -> None:
    """重复分享复活同一行，避免唯一键冲突。"""
    stmt = mysql_insert(ConversationShare).values(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        grantee_user_id=grantee_user_id,
        permission=PERMISSION_READ,
        created_by=created_by,
        revoked_at=None,
    )
    stmt = stmt.on_duplicate_key_update(
        permission=PERMISSION_READ,
        created_by=created_by,
        revoked_at=null(),
    )
    await session.execute(stmt)


async def revoke_share(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    grantee_user_id: int,
    owner_user_id: int | None,
    revoked_at: datetime,
) -> None:
    """只有 Owner 创建的、尚未撤销的分享可以被取消。"""
    await session.execute(
        update(ConversationShare)
        .where(
            ConversationShare.tenant_id == tenant_id,
            ConversationShare.conversation_id == conversation_id,
            ConversationShare.grantee_user_id == grantee_user_id,
            ConversationShare.created_by == owner_user_id,
            ConversationShare.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )


async def find_elicitation(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    request_id: str,
) -> AgentConversationElicitation | None:
    """按会话和请求号取问答卡片。"""
    return await session.scalar(
        select(AgentConversationElicitation)
        .where(
            AgentConversationElicitation.tenant_id == tenant_id,
            AgentConversationElicitation.conversation_id == conversation_id,
            AgentConversationElicitation.request_id == request_id,
        )
        .limit(1)
    )


async def list_pending_elicitations(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> list[AgentConversationElicitation]:
    """会话里尚未解决的问答卡片。"""
    stmt = (
        select(AgentConversationElicitation)
        .where(
            AgentConversationElicitation.tenant_id == tenant_id,
            AgentConversationElicitation.conversation_id == conversation_id,
            AgentConversationElicitation.status == "PENDING",
        )
        .order_by(AgentConversationElicitation.id.asc())
    )
    return list((await session.scalars(stmt)).all())


async def list_pending_by_turn(
    session: AsyncSession,
    tenant_id: int,
    turn_id: int,
) -> list[AgentConversationElicitation]:
    """某一轮上尚未解决的问答卡片。"""
    stmt = (
        select(AgentConversationElicitation)
        .where(
            AgentConversationElicitation.tenant_id == tenant_id,
            AgentConversationElicitation.turn_id == turn_id,
            AgentConversationElicitation.status == "PENDING",
        )
        .order_by(AgentConversationElicitation.id.asc())
    )
    return list((await session.scalars(stmt)).all())


async def settle_if_pending(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    request_id: str,
    status: str,
    answer_json: str | None,
) -> int:
    """只把仍挂起的卡片改成终态。返回 1 表示这次抢到了转移。"""
    result = await session.execute(
        update(AgentConversationElicitation)
        .where(
            AgentConversationElicitation.tenant_id == tenant_id,
            AgentConversationElicitation.conversation_id == conversation_id,
            AgentConversationElicitation.request_id == request_id,
            AgentConversationElicitation.status == "PENDING",
        )
        .values(status=status, answer_json=answer_json, gmt_modified=func.now())
    )
    return rowcount(result)


async def restore_pending_if_status(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    request_id: str,
    expected_status: str,
) -> int:
    """投递失败后，只把仍停在自己刚写下的终态的卡片补回挂起。"""
    result = await session.execute(
        update(AgentConversationElicitation)
        .where(
            AgentConversationElicitation.tenant_id == tenant_id,
            AgentConversationElicitation.conversation_id == conversation_id,
            AgentConversationElicitation.request_id == request_id,
            AgentConversationElicitation.status == expected_status,
        )
        .values(status="PENDING", answer_json=None, gmt_modified=func.now())
    )
    return rowcount(result)
