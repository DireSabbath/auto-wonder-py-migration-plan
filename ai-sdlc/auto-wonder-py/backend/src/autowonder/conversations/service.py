"""平台管家对话和工单澄清会话的业务入口。"""

import logging
import re
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.conversations.access import is_owner, require_body_read, require_body_write
from autowonder.conversations.commands import refresh as refresh_commands
from autowonder.conversations.commands import snapshot as command_snapshot
from autowonder.conversations.constants import (
    AUTO_TITLE_LENGTH,
    BIZ_REF_WORKITEM,
    CLARIFICATION_CHANNEL,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    MAX_TITLE_LENGTH,
    PLATFORM_CHANNEL,
    STATUS_ACTIVE,
    TITLE_SOURCE_AUTO,
    TITLE_SOURCE_USER,
)
from autowonder.conversations.elicitation import list_pending, reply
from autowonder.conversations.events import list_after, list_for_turn
from autowonder.conversations.models import AgentConversation, AgentConversationTurn
from autowonder.conversations.records import (
    find_agent,
    find_conversation,
    find_next_queued_inbound,
    find_processing_inbound,
    insert_conversation,
    list_by_biz_ref,
    list_platform_by_owner,
    list_turns,
    mark_platform_deleted,
    update_executor,
    update_platform_metadata,
)
from autowonder.conversations.routing import (
    ProtocolUnsupported,
    executor_online,
    protocol_features,
    protocol_supported,
    runtime_capabilities,
    select_executor,
)
from autowonder.conversations.schemas import (
    ClarificationConversationView,
    ElicitationView,
    PlatformConversationView,
    ShareView,
    SlashCommandView,
    TurnEventView,
    TurnView,
)
from autowonder.conversations.shares import grant_read_share, list_shares, revoke_read_share
from autowonder.conversations.turns import (
    clarification_external_message_id,
    platform_external_message_id,
    platform_message_token,
    request_turn_cancel,
    require_selected_executor,
    resolve_processing,
    submit_inbound,
)
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode, IllegalArgumentError

logger = logging.getLogger(__name__)


def require_owner_user(owner_user_id: int | None) -> int:
    """没有合法调用者时按未登录拒绝，不建会话。"""
    if owner_user_id is None or owner_user_id <= 0:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return owner_user_id


def normalize_user_title(title: str | None) -> str | None:
    """去掉首尾空白。空白变成没有标题；超过 255 个字符不合法。"""
    if title is None or title.strip() == "":
        return None
    trimmed = title.strip()
    if len(trimmed) > MAX_TITLE_LENGTH:
        raise BizError(ErrorCode.PLATFORM_CONVERSATION_TITLE_INVALID)
    return trimmed


def auto_title(content: str) -> str:
    """用首条消息生成不超过 30 个字符的标题，更长时加省略号。"""
    flattened = re.sub(r"\s+", " ", content.strip())
    if len(flattened) <= AUTO_TITLE_LENGTH:
        return flattened
    return flattened[:AUTO_TITLE_LENGTH] + "…"


def should_apply_auto_title(title_source: str | None, title: str | None) -> bool:
    """用户改过名，或已经有标题时，不再用消息覆盖。"""
    if title_source == TITLE_SOURCE_USER:
        return False
    if title is not None and title.strip() != "":
        return False
    return True


def normalize_page_size(page_size: int | None) -> int:
    """缺省或非正数用 50，超过 200 时截到 200。"""
    if page_size is None or page_size <= 0:
        return DEFAULT_PAGE_SIZE
    if page_size > MAX_PAGE_SIZE:
        return MAX_PAGE_SIZE
    return page_size


def page_offset(page: int | None, limit: int) -> int:
    """页码从 1 开始。缺省、0 和负数都落在第一页。"""
    index = 0 if page is None else page - 1
    if index < 0:
        index = 0
    return index * limit


def normalize_keyword(keyword: str | None) -> str | None:
    """空白关键字等于不筛选。"""
    if keyword is None or keyword.strip() == "":
        return None
    return keyword.strip()


def archive_moment(
    archived: bool | None,
    current: datetime | None,
    now: datetime,
) -> datetime | None:
    """None 保持原归档时间，true 记现在，false 清空。"""
    if archived is None:
        return current
    if archived:
        return now
    return None


def belongs_to_workitem(
    channel: str | None,
    biz_ref_type: str | None,
    biz_ref_id: int | None,
    workitem_id: int,
) -> bool:
    """澄清会话必须属于这条工单。"""
    return (
        channel == CLARIFICATION_CHANNEL
        and biz_ref_type == BIZ_REF_WORKITEM
        and biz_ref_id == workitem_id
    )


async def create_platform_conversation(
    session: AsyncSession,
    tenant_id: int,
    owner_user_id: int | None,
    agent_id: int | None,
    title: str | None,
) -> PlatformConversationView:
    """新建会话，调用者即不可变更的 Owner。"""
    owner = require_owner_user(owner_user_id)
    agent = await _require_usable_chief(session, tenant_id, agent_id)
    normalized = normalize_user_title(title)
    executor_id = await _select(session, tenant_id, agent.id, agent.online_version_id, None)
    conversation = AgentConversation(
        tenant_id=tenant_id,
        owner_user_id=owner,
        agent_id=agent.id,
        channel=PLATFORM_CHANNEL,
        channel_conversation_id=str(uuid.uuid4()),
        title=normalized,
        title_source=None if normalized is None else TITLE_SOURCE_USER,
        agent_version_id=agent.online_version_id,
        executor_id=executor_id,
        status=STATUS_ACTIVE,
        last_turn_at=now_local(),
    )
    await insert_conversation(session, conversation)
    await session.commit()
    return await _platform_summary(session, conversation, owner, {})


async def list_platform_conversations(
    session: AsyncSession,
    tenant_id: int,
    owner_user_id: int,
    archived: bool | None,
    keyword: str | None,
    page_size: int | None,
    page: int | None,
) -> list[PlatformConversationView]:
    """分页列出自己的平台管家会话。"""
    limit = normalize_page_size(page_size)
    rows = await list_platform_by_owner(
        session,
        tenant_id,
        owner_user_id,
        archived,
        normalize_keyword(keyword),
        limit,
        page_offset(page, limit),
    )
    cache: dict[int, Agent | None] = {}
    views: list[PlatformConversationView] = []
    for row in rows:
        views.append(await _platform_summary(session, row, owner_user_id, cache))
    return views


async def get_platform_conversation(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
) -> PlatformConversationView:
    """详情带轮次、挂起卡片和命令。分享名单只给 Owner。"""
    conversation = await require_body_read(session, tenant_id, conversation_id, user_id)
    view = await _platform_summary(session, conversation, user_id, {})
    view.turns = [_turn_view(row) for row in await list_turns(session, tenant_id, conversation_id)]
    view.pending_elicitations = await list_pending(session, tenant_id, conversation_id)
    view.available_commands = await command_snapshot(tenant_id, conversation_id)
    processing = await find_processing_inbound(session, tenant_id, conversation_id)
    queued = None
    if processing is None:
        queued = await find_next_queued_inbound(session, tenant_id, conversation_id)
    status, turn_id = resolve_processing(
        None if processing is None else processing.status,
        None if processing is None else processing.id,
        None if queued is None else queued.status,
        None if queued is None else queued.id,
    )
    view.processing_status = status
    view.processing_turn_id = turn_id
    if is_owner(conversation.owner_user_id, user_id):
        view.shares = await list_shares(session, tenant_id, conversation_id)
    return view


async def patch_platform_conversation(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
    title: str | None,
    archived: bool | None,
) -> PlatformConversationView:
    """改名和归档同一次写入。两个字段都缺省时不碰库。"""
    conversation = await require_body_write(session, tenant_id, conversation_id, user_id)
    if title is None and archived is None:
        return await _platform_summary(session, conversation, user_id, {})
    next_title = None
    next_source = conversation.title_source
    if title is not None:
        next_title = normalize_user_title(title)
        if next_title is None:
            raise BizError(ErrorCode.PLATFORM_CONVERSATION_TITLE_INVALID)
        next_source = TITLE_SOURCE_USER
    next_archived = archive_moment(archived, conversation.archived_at, now_local())
    await update_platform_metadata(
        session,
        tenant_id,
        conversation_id,
        conversation.owner_user_id,
        next_title,
        next_source,
        next_archived,
    )
    if next_title is not None:
        conversation.title = next_title
        conversation.title_source = TITLE_SOURCE_USER
    conversation.archived_at = next_archived
    await session.commit()
    return await _platform_summary(session, conversation, user_id, {})


async def delete_platform_conversation(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
) -> None:
    """软删除。之后读写都表现为不存在。"""
    conversation = await require_body_write(session, tenant_id, conversation_id, user_id)
    await mark_platform_deleted(
        session, tenant_id, conversation_id, conversation.owner_user_id, now_local()
    )
    await session.commit()


async def list_platform_shares(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
) -> list[ShareView]:
    """分享名单只给 Owner 看。"""
    await require_body_write(session, tenant_id, conversation_id, user_id)
    return await list_shares(session, tenant_id, conversation_id)


async def share_platform_conversation(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
    grantee_user_id: int | None,
) -> list[ShareView]:
    """只读分享给同工作空间成员。"""
    conversation = await require_body_write(session, tenant_id, conversation_id, user_id)
    return await grant_read_share(session, tenant_id, conversation, grantee_user_id)


async def revoke_platform_share(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
    grantee_user_id: int,
) -> list[ShareView]:
    """取消一名被分享人。"""
    conversation = await require_body_write(session, tenant_id, conversation_id, user_id)
    return await revoke_read_share(session, tenant_id, conversation, grantee_user_id)


async def submit_platform_turn(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
    content: str | None,
    client_message_id: str | None,
) -> None:
    """Owner 在未归档的活跃会话里发消息，并在需要时补自动标题。"""
    conversation = await require_body_write(session, tenant_id, conversation_id, user_id)
    if conversation.archived_at is not None:
        raise BizError(ErrorCode.PLATFORM_CONVERSATION_ARCHIVED)
    if conversation.status != STATUS_ACTIVE:
        raise BizError(ErrorCode.PLATFORM_CONVERSATION_DELETED)
    if content is None or content.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID, "消息内容不能为空")
    agent = await _require_usable_chief(session, tenant_id, conversation.agent_id)
    executor_id = await _select(
        session,
        tenant_id,
        conversation.agent_id,
        agent.online_version_id,
        conversation.executor_id,
    )
    bound = require_selected_executor(executor_id)
    if bound != conversation.executor_id:
        await update_executor(session, tenant_id, conversation_id, bound)
        conversation.executor_id = bound
    token = platform_message_token(client_message_id, str(uuid.uuid4()))
    await submit_inbound(
        session,
        tenant_id,
        conversation.agent_id,
        PLATFORM_CHANNEL,
        conversation.channel_conversation_id,
        content,
        platform_external_message_id(conversation_id, token),
    )
    if should_apply_auto_title(conversation.title_source, conversation.title):
        await update_platform_metadata(
            session,
            tenant_id,
            conversation_id,
            conversation.owner_user_id,
            auto_title(content),
            TITLE_SOURCE_AUTO,
            conversation.archived_at,
        )
        await session.commit()


async def cancel_platform_turn(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
    turn_id: int,
) -> None:
    """终止一轮回复。先确认是 Owner。"""
    await require_body_write(session, tenant_id, conversation_id, user_id)
    await request_turn_cancel(session, tenant_id, conversation_id, turn_id)


async def reply_platform_elicitation(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
    request_id: str,
    action: str | None,
    content: str | None,
) -> None:
    """回答卡片。只认 Owner。"""
    await require_body_write(session, tenant_id, conversation_id, user_id)
    await reply(session, tenant_id, conversation_id, request_id, action, content)


async def refresh_platform_commands(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
) -> None:
    """可读的人打开会话时刷新命令探针。"""
    await require_body_read(session, tenant_id, conversation_id, user_id)
    await refresh_commands(session, tenant_id, conversation_id)


async def list_platform_events(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
    after_id: int,
) -> list[TurnEventView]:
    """校验可读后按 afterId 拉事件。"""
    await require_body_read(session, tenant_id, conversation_id, user_id)
    return await list_after(session, tenant_id, conversation_id, after_id)


async def list_platform_turn_events(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
    user_id: int,
    turn_id: int,
) -> list[TurnEventView]:
    """校验可读后按轮次拉事件。"""
    await require_body_read(session, tenant_id, conversation_id, user_id)
    return await list_for_turn(session, tenant_id, conversation_id, turn_id)


async def list_clarification_conversations(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    agent_id: int,
) -> list[ClarificationConversationView]:
    """列出工单上某个数字人的澄清会话。"""
    rows = await list_by_biz_ref(
        session, tenant_id, CLARIFICATION_CHANNEL, BIZ_REF_WORKITEM, workitem_id, agent_id
    )
    views: list[ClarificationConversationView] = []
    for row in rows:
        views.append(await _clarification_view(session, row, None, [], []))
    return views


async def create_clarification_conversation(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    agent_id: int | None,
) -> ClarificationConversationView:
    """新建澄清会话。数字人不存在，或没有在线版本时拒绝。"""
    if agent_id is None:
        raise IllegalArgumentError("agent not found")
    agent = await find_agent(session, agent_id)
    if agent is None or agent.is_deleted == 1:
        raise IllegalArgumentError("agent not found")
    if agent.online_version_id is None:
        raise BizError(ErrorCode.SYSTEM_ERROR)
    executor_id = await _select(session, tenant_id, agent_id, agent.online_version_id, None)
    conversation = AgentConversation(
        tenant_id=tenant_id,
        agent_id=agent_id,
        agent_version_id=agent.online_version_id,
        channel=CLARIFICATION_CHANNEL,
        biz_ref_type=BIZ_REF_WORKITEM,
        biz_ref_id=workitem_id,
        channel_conversation_id=str(uuid.uuid4()),
        executor_id=executor_id,
        status=STATUS_ACTIVE,
        last_turn_at=now_local(),
    )
    await insert_conversation(session, conversation)
    await session.commit()
    return await _clarification_view(session, conversation, [], [], [])


async def get_clarification_conversation(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    conversation_id: int,
) -> ClarificationConversationView:
    """详情带历史。卡片或命令失败时降级为空，不挡住已有轮次。"""
    conversation = await _require_clarification(session, tenant_id, workitem_id, conversation_id)
    turns = [_turn_view(row) for row in await list_turns(session, tenant_id, conversation_id)]
    processing = await find_processing_inbound(session, tenant_id, conversation_id)
    queued = None
    if processing is None:
        queued = await find_next_queued_inbound(session, tenant_id, conversation_id)
    view = await _clarification_view(
        session,
        conversation,
        turns,
        await _pending_quietly(session, tenant_id, conversation_id),
        await command_snapshot(tenant_id, conversation_id),
    )
    status, turn_id = resolve_processing(
        None if processing is None else processing.status,
        None if processing is None else processing.id,
        None if queued is None else queued.status,
        None if queued is None else queued.id,
    )
    view.processing_status = status
    view.processing_turn_id = turn_id
    return view


async def submit_clarification_turn(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    conversation_id: int,
    content: str | None,
    client_message_id: str | None,
) -> None:
    """确认会话属于工单后提交一轮。"""
    conversation = await _require_clarification(session, tenant_id, workitem_id, conversation_id)
    agent = await find_agent(session, conversation.agent_id)
    if agent is None or agent.is_deleted == 1 or agent.online_version_id is None:
        raise BizError(ErrorCode.SYSTEM_ERROR)
    executor_id = await _select(
        session,
        tenant_id,
        conversation.agent_id,
        agent.online_version_id,
        conversation.executor_id,
    )
    bound = require_selected_executor(executor_id)
    if bound != conversation.executor_id:
        await update_executor(session, tenant_id, conversation_id, bound)
        conversation.executor_id = bound
    await submit_inbound(
        session,
        tenant_id,
        conversation.agent_id,
        CLARIFICATION_CHANNEL,
        conversation.channel_conversation_id,
        content,
        clarification_external_message_id(client_message_id),
    )


async def cancel_clarification_turn(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    conversation_id: int,
    turn_id: int,
) -> None:
    """确认归属后终止一轮澄清回复。"""
    await _require_clarification(session, tenant_id, workitem_id, conversation_id)
    await request_turn_cancel(session, tenant_id, conversation_id, turn_id)


async def reply_clarification_elicitation(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    conversation_id: int,
    request_id: str,
    action: str | None,
    content: str | None,
) -> None:
    """先确认会话属于工单，再回答卡片。"""
    await _require_clarification(session, tenant_id, workitem_id, conversation_id)
    await reply(session, tenant_id, conversation_id, request_id, action, content)


async def refresh_clarification_commands(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    conversation_id: int,
) -> None:
    """确认归属后刷新命令探针。"""
    await _require_clarification(session, tenant_id, workitem_id, conversation_id)
    await refresh_commands(session, tenant_id, conversation_id)


async def list_clarification_events(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    conversation_id: int,
    after_id: int,
) -> list[TurnEventView]:
    """确认归属后按 afterId 拉事件。"""
    await _require_clarification(session, tenant_id, workitem_id, conversation_id)
    return await list_after(session, tenant_id, conversation_id, after_id)


async def list_clarification_turn_events(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    conversation_id: int,
    turn_id: int,
) -> list[TurnEventView]:
    """确认归属后按轮次拉事件。"""
    await _require_clarification(session, tenant_id, workitem_id, conversation_id)
    return await list_for_turn(session, tenant_id, conversation_id, turn_id)


async def _require_clarification(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
    conversation_id: int,
) -> AgentConversation:
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if conversation is None:
        raise IllegalArgumentError("conversation not found")
    if not belongs_to_workitem(
        conversation.channel,
        conversation.biz_ref_type,
        conversation.biz_ref_id,
        workitem_id,
    ):
        raise IllegalArgumentError("conversation does not belong to this workitem")
    return conversation


async def _require_usable_chief(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int | None,
) -> Agent:
    if agent_id is None:
        raise BizError(ErrorCode.AGENT_NOT_FOUND)
    agent = await find_agent(session, agent_id)
    if agent is None or agent.tenant_id != tenant_id or agent.is_deleted == 1:
        raise BizError(ErrorCode.AGENT_NOT_FOUND)
    if agent.online_version_id is None:
        raise BizError(ErrorCode.PLATFORM_CONVERSATION_NOT_READY)
    return agent


async def _select(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    agent_version_id: int | None,
    preferred_executor_id: int | None,
) -> int | None:
    if agent_version_id is None:
        raise BizError(ErrorCode.SYSTEM_ERROR)
    try:
        return await select_executor(
            session, tenant_id, agent_id, agent_version_id, preferred_executor_id
        )
    except ProtocolUnsupported as error:
        raise BizError(ErrorCode.SYSTEM_ERROR) from error


async def _platform_summary(
    session: AsyncSession,
    conversation: AgentConversation,
    user_id: int,
    cache: dict[int, Agent | None],
) -> PlatformConversationView:
    agent = await _cached_agent(session, cache, conversation.agent_id)
    online = executor_online(conversation.executor_id)
    features = protocol_features(conversation.executor_id)
    capabilities = runtime_capabilities(online, features)
    name = None
    if agent is not None and agent.is_deleted != 1:
        name = agent.name
    return PlatformConversationView(
        id=conversation.id,
        owner_user_id=conversation.owner_user_id,
        owner=is_owner(conversation.owner_user_id, user_id),
        agent_id=conversation.agent_id,
        agent_name=name,
        channel_conversation_id=conversation.channel_conversation_id,
        title=conversation.title,
        title_source=conversation.title_source,
        status=conversation.status,
        executor_online=online,
        streaming_supported=capabilities["streaming_supported"],
        cancel_supported=capabilities["cancel_supported"],
        acp_interaction_supported=capabilities["acp_interaction_supported"],
        attachment_manifest_supported=capabilities["attachment_manifest_supported"],
        artifact_output_supported=capabilities["artifact_output_supported"],
        action_plan_supported=capabilities["action_plan_supported"],
        cli_session_ref=conversation.cli_session_ref,
        archived_at=conversation.archived_at,
        last_turn_at=conversation.last_turn_at,
        gmt_create=conversation.gmt_create,
    )


async def _clarification_view(
    session: AsyncSession,
    conversation: AgentConversation,
    turns: list[TurnView] | None,
    pending: list[ElicitationView],
    commands: list[SlashCommandView],
) -> ClarificationConversationView:
    agent = await find_agent(session, conversation.agent_id)
    name = None
    if agent is not None and agent.is_deleted != 1:
        name = agent.name
    online = executor_online(conversation.executor_id)
    features = protocol_features(conversation.executor_id)
    return ClarificationConversationView(
        id=conversation.id,
        agent_id=conversation.agent_id,
        agent_name=name,
        channel_conversation_id=conversation.channel_conversation_id,
        status=conversation.status,
        executor_online=online,
        streaming_supported=protocol_supported(online, features, "CONVERSATION_TURN_EVENT"),
        cancel_supported=protocol_supported(online, features, "CONVERSATION_TURN_CANCEL"),
        acp_interaction_supported=protocol_supported(
            online, features, "CONVERSATION_ACP_INTERACTION_V1"
        ),
        cli_session_ref=conversation.cli_session_ref,
        last_turn_at=conversation.last_turn_at,
        gmt_create=conversation.gmt_create,
        turns=turns,
        pending_elicitations=pending,
        available_commands=commands,
    )


async def _cached_agent(
    session: AsyncSession,
    cache: dict[int, Agent | None],
    agent_id: int,
) -> Agent | None:
    if agent_id not in cache:
        cache[agent_id] = await find_agent(session, agent_id)
    return cache[agent_id]


async def _pending_quietly(
    session: AsyncSession,
    tenant_id: int,
    conversation_id: int,
) -> list[ElicitationView]:
    try:
        return await list_pending(session, tenant_id, conversation_id)
    except Exception:
        logger.warning(
            "pending elicitations degraded tenantId=%s conversationId=%s",
            tenant_id,
            conversation_id,
        )
        return []


def _turn_view(row: AgentConversationTurn) -> TurnView:
    return TurnView(
        id=row.id,
        direction=row.direction,
        content=row.content,
        status=row.status,
        error=row.error,
        gmt_create=row.gmt_create,
    )
