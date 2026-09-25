"""平台管家对话与工单澄清会话的 HTTP 入口。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.conversations.schemas import (
    ClarificationConversationRequest,
    ClarificationTurnRequest,
    ElicitationReplyRequest,
    PlatformConversationPatchRequest,
    PlatformConversationRequest,
    PlatformShareRequest,
    PlatformTurnRequest,
)
from autowonder.conversations.service import (
    cancel_clarification_turn,
    cancel_platform_turn,
    create_clarification_conversation,
    create_platform_conversation,
    delete_platform_conversation,
    get_clarification_conversation,
    get_platform_conversation,
    list_clarification_conversations,
    list_clarification_events,
    list_clarification_turn_events,
    list_platform_conversations,
    list_platform_events,
    list_platform_shares,
    list_platform_turn_events,
    patch_platform_conversation,
    refresh_clarification_commands,
    refresh_platform_commands,
    reply_clarification_elicitation,
    reply_platform_elicitation,
    revoke_platform_share,
    share_platform_conversation,
    submit_clarification_turn,
    submit_platform_turn,
)
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session

platform_router = APIRouter(
    prefix="/api/platform/conversations",
    tags=["platform-conversations"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看平台管家对话"))],
)

clarification_router = APIRouter(
    prefix="/api/workitems/{workitemId}/clarification-conversations",
    tags=["clarification-conversations"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看工单澄清会话"))],
)


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


@platform_router.get("")
async def list_platform(
    archived: Annotated[bool | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
    page_size: Annotated[int | None, Query(alias="pageSize")] = None,
    page: Annotated[int | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """分页列出当前用户的平台管家会话。"""
    return ok(
        await list_platform_conversations(
            session,
            _workspace_id(),
            _user_id(),
            archived,
            keyword,
            page_size,
            page,
        )
    )


@platform_router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "新建平台管家对话"))],
)
async def create_platform(
    body: PlatformConversationRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """新建平台管家会话。"""
    return ok(
        await create_platform_conversation(
            session, _workspace_id(), _user_id(), body.agent_id, body.title
        )
    )


@platform_router.get("/{conversationId}")
async def get_platform(
    conversationId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """读取一条平台管家会话。"""
    return ok(
        await get_platform_conversation(session, _workspace_id(), conversationId, _user_id())
    )


@platform_router.patch(
    "/{conversationId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "修改平台管家对话"))],
)
async def patch_platform(
    conversationId: int,
    body: PlatformConversationPatchRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """同一次改名和归档。"""
    return ok(
        await patch_platform_conversation(
            session, _workspace_id(), conversationId, _user_id(), body.title, body.archived
        )
    )


@platform_router.delete(
    "/{conversationId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除平台管家对话"))],
)
async def delete_platform(
    conversationId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除一条平台管家会话。"""
    await delete_platform_conversation(session, _workspace_id(), conversationId, _user_id())
    return ok(None)


@platform_router.get("/{conversationId}/shares")
async def list_shares(
    conversationId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """列出会话的只读分享。"""
    return ok(await list_platform_shares(session, _workspace_id(), conversationId, _user_id()))


@platform_router.post(
    "/{conversationId}/shares",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "分享平台管家对话"))],
)
async def share_platform(
    conversationId: int,
    body: PlatformShareRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把会话只读分享给一名成员。"""
    return ok(
        await share_platform_conversation(
            session, _workspace_id(), conversationId, _user_id(), body.grantee_user_id
        )
    )


@platform_router.delete(
    "/{conversationId}/shares/{granteeUserId}",
    dependencies=[
        Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "取消分享平台管家对话"))
    ],
)
async def revoke_share(
    conversationId: int,
    granteeUserId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """取消一名被分享人。"""
    return ok(
        await revoke_platform_share(
            session, _workspace_id(), conversationId, _user_id(), granteeUserId
        )
    )


@platform_router.get("/{conversationId}/events")
async def platform_events(
    conversationId: int,
    after_id: Annotated[int, Query(alias="afterId")] = 0,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """增量拉取会话事件。"""
    return ok(
        await list_platform_events(session, _workspace_id(), conversationId, _user_id(), after_id)
    )


@platform_router.get("/{conversationId}/turns/{turnId}/events")
async def platform_turn_events(
    conversationId: int,
    turnId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按轮次拉取执行事件。"""
    return ok(
        await list_platform_turn_events(
            session, _workspace_id(), conversationId, _user_id(), turnId
        )
    )


@platform_router.post(
    "/{conversationId}/turns",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "发送平台管家消息"))],
)
async def platform_turn(
    conversationId: int,
    body: PlatformTurnRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """发送一条平台管家消息。"""
    await submit_platform_turn(
        session,
        _workspace_id(),
        conversationId,
        _user_id(),
        body.content,
        body.client_message_id,
    )
    return ok(None)


@platform_router.post(
    "/{conversationId}/turns/{turnId}/cancel",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "终止平台管家回复"))],
)
async def platform_cancel(
    conversationId: int,
    turnId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """终止一轮平台管家回复。"""
    await cancel_platform_turn(session, _workspace_id(), conversationId, _user_id(), turnId)
    return ok(None)


@platform_router.post(
    "/{conversationId}/elicitations/{requestId}/reply",
    dependencies=[
        Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "回答平台管家问题卡片"))
    ],
)
async def platform_reply(
    conversationId: int,
    requestId: str,
    body: ElicitationReplyRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """回答平台管家的问答卡片。"""
    await reply_platform_elicitation(
        session,
        _workspace_id(),
        conversationId,
        _user_id(),
        requestId,
        body.action,
        body.content,
    )
    return ok(None)


@platform_router.post("/{conversationId}/commands/refresh")
async def platform_refresh(
    conversationId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """刷新平台管家会话的斜杠命令。"""
    await refresh_platform_commands(session, _workspace_id(), conversationId, _user_id())
    return ok(None)


@clarification_router.get("")
async def list_clarification(
    workitemId: int,
    agentId: Annotated[int, Query(alias="agentId")],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """列出工单上的澄清会话。"""
    _user_id()
    return ok(
        await list_clarification_conversations(session, _workspace_id(), workitemId, agentId)
    )


@clarification_router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建工单澄清会话"))],
)
async def create_clarification(
    workitemId: int,
    body: ClarificationConversationRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """创建工单澄清会话。"""
    _user_id()
    return ok(
        await create_clarification_conversation(
            session, _workspace_id(), workitemId, body.agent_id
        )
    )


@clarification_router.get("/{conversationId}")
async def get_clarification(
    workitemId: int,
    conversationId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """读取一条工单澄清会话。"""
    _user_id()
    return ok(
        await get_clarification_conversation(session, _workspace_id(), workitemId, conversationId)
    )


@clarification_router.get("/{conversationId}/events")
async def clarification_events(
    workitemId: int,
    conversationId: int,
    after_id: Annotated[int, Query(alias="afterId")] = 0,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """增量拉取澄清会话事件。"""
    _user_id()
    return ok(
        await list_clarification_events(
            session, _workspace_id(), workitemId, conversationId, after_id
        )
    )


@clarification_router.post(
    "/{conversationId}/turns",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "发送工单澄清消息"))],
)
async def clarification_turn(
    workitemId: int,
    conversationId: int,
    body: ClarificationTurnRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """发送一条工单澄清消息。"""
    _user_id()
    await submit_clarification_turn(
        session,
        _workspace_id(),
        workitemId,
        conversationId,
        body.content,
        body.client_message_id,
    )
    return ok(None)


@clarification_router.post(
    "/{conversationId}/turns/{turnId}/cancel",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "终止工单澄清回复"))],
)
async def clarification_cancel(
    workitemId: int,
    conversationId: int,
    turnId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """终止一轮工单澄清回复。"""
    _user_id()
    await cancel_clarification_turn(session, _workspace_id(), workitemId, conversationId, turnId)
    return ok(None)


@clarification_router.post(
    "/{conversationId}/elicitations/{requestId}/reply",
    dependencies=[
        Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "回答工单澄清问题卡片"))
    ],
)
async def clarification_reply(
    workitemId: int,
    conversationId: int,
    requestId: str,
    body: ElicitationReplyRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """回答工单澄清的问答卡片。"""
    _user_id()
    await reply_clarification_elicitation(
        session,
        _workspace_id(),
        workitemId,
        conversationId,
        requestId,
        body.action,
        body.content,
    )
    return ok(None)


@clarification_router.get("/{conversationId}/turns/{turnId}/events")
async def clarification_turn_events(
    workitemId: int,
    conversationId: int,
    turnId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按轮次拉取澄清执行事件。"""
    _user_id()
    return ok(
        await list_clarification_turn_events(
            session, _workspace_id(), workitemId, conversationId, turnId
        )
    )


@clarification_router.post("/{conversationId}/commands/refresh")
async def clarification_refresh(
    workitemId: int,
    conversationId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """刷新工单澄清会话的斜杠命令。"""
    _user_id()
    await refresh_clarification_commands(session, _workspace_id(), workitemId, conversationId)
    return ok(None)
