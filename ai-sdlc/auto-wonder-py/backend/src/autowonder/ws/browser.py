"""浏览器实时通道。路径 ``/ws``，首帧必须是 ``auth``。

订阅按频道前缀授权。成员是否有效跟表注释一致：``status = 0`` 为正常。
Java 实时授权把状态写成 1，那会拒绝全部正常成员。
"""

import logging
from dataclasses import dataclass

from fastapi import APIRouter
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from autowonder.api.access import WorkspaceAccessLevel
from autowonder.conversations.models import AgentConversation
from autowonder.db.session import SessionLocal
from autowonder.dispatch.models import Dispatch
from autowonder.scheduledtasks.capability import require_scheduled_capability
from autowonder.scheduledtasks.models import ScheduledTaskRun
from autowonder.security.jwt import parse_access
from autowonder.workspaces.models import OrgMember
from autowonder.ws.frames import VIOLATED_POLICY_CLOSE_CODE

logger = logging.getLogger(__name__)

router = APIRouter()
MEMBER_STATUS_ACTIVE = 0
DISPATCH_PREFIX = "dispatch:"
CONVERSATION_PREFIX = "conversation:"
SCHEDULED_RUN_PREFIX = "scheduled-run:"


@dataclass
class Principal:
    """一条浏览器连接上的工作空间和用户。"""

    tenant_id: int
    user_id: int


class BrowserHub:
    """浏览器连接上的频道订阅。"""

    def __init__(self) -> None:
        self.principals: dict[WebSocket, Principal] = {}
        self.channels_by_session: dict[WebSocket, set[str]] = {}
        self.sessions_by_channel: dict[str, set[WebSocket]] = {}
        self.workspaces: dict[WebSocket, int] = {}

    def record_principal(self, websocket: WebSocket, tenant_id: int, user_id: int) -> None:
        """鉴权成功后记下身份。"""
        self.principals[websocket] = Principal(tenant_id, user_id)
        self.workspaces[websocket] = tenant_id

    def principal(self, websocket: WebSocket) -> Principal | None:
        """这条连接的身份。还没鉴权时没有。"""
        return self.principals.get(websocket)

    def add_subscription(self, websocket: WebSocket, channel: str) -> None:
        """把连接加进频道。"""
        self.channels_by_session.setdefault(websocket, set()).add(channel)
        self.sessions_by_channel.setdefault(channel, set()).add(websocket)
        logger.debug("subscription added channel=%s", channel)

    def remove_subscription(self, websocket: WebSocket, channel: str) -> None:
        """取消一个频道。"""
        channels = self.channels_by_session.get(websocket)
        if channels is not None:
            channels.discard(channel)
        sessions = self.sessions_by_channel.get(channel)
        if sessions is not None:
            sessions.discard(websocket)
            if len(sessions) == 0:
                del self.sessions_by_channel[channel]

    def remove_session(self, websocket: WebSocket) -> None:
        """连接断开时摘掉它的全部订阅。"""
        self.principals.pop(websocket, None)
        self.workspaces.pop(websocket, None)
        channels = self.channels_by_session.pop(websocket, None)
        if channels is None:
            return
        for channel in channels:
            sessions = self.sessions_by_channel.get(channel)
            if sessions is None:
                continue
            sessions.discard(websocket)
            if len(sessions) == 0:
                del self.sessions_by_channel[channel]


browser_hub = BrowserHub()


@router.websocket("/ws")
async def browser_socket(websocket: WebSocket) -> None:
    """浏览器先发 auth，通过后再 subscribe / unsubscribe。"""
    await websocket.accept()
    from autowonder.ws.runtime import ensure_listeners

    ensure_listeners()
    logger.info("browser realtime connected")
    authenticated = False
    try:
        while True:
            try:
                message = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            authenticated = await _on_message(websocket, message, authenticated)
            if websocket.client_state != WebSocketState.CONNECTED:
                return
    finally:
        browser_hub.remove_session(websocket)
        logger.info("browser realtime disconnected")


async def deliver_browser_channel(channel: str, frame_json: str) -> None:
    """把一帧发给订阅了该频道且仍然打开的浏览器。"""
    sessions = browser_hub.sessions_by_channel.get(channel)
    if sessions is None or len(sessions) == 0:
        return
    for websocket in list(sessions):
        if websocket.client_state != WebSocketState.CONNECTED:
            browser_hub.remove_session(websocket)
            continue
        try:
            await websocket.send_text(frame_json)
        except Exception:
            logger.warning("subscriber delivery failed channel=%s", channel)
            browser_hub.remove_session(websocket)


async def _on_message(websocket: WebSocket, message: str, authenticated: bool) -> bool:
    import json

    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        logger.warning("browser realtime message failed")
        return authenticated
    if parsed is None:
        await _reject(websocket, "invalid frame")
        return authenticated
    if not isinstance(parsed, dict):
        logger.warning("browser realtime message failed")
        return authenticated
    frame_type = parsed.get("type")
    if not authenticated:
        if frame_type != "auth":
            await _reject(websocket, "auth required")
            return authenticated
        return await _auth(websocket, parsed)
    if frame_type == "subscribe":
        await _subscribe(websocket, parsed)
    elif frame_type == "unsubscribe":
        _unsubscribe(websocket, parsed)
    else:
        logger.debug("browser realtime unknown frame type=%s", frame_type)
    return True


async def _auth(websocket: WebSocket, payload: dict[str, object]) -> bool:
    token = payload.get("token")
    if not isinstance(token, str) or token.strip() == "":
        await _reject(websocket, "missing token")
        return False
    try:
        claims = parse_access(token)
    except (InvalidTokenError, KeyError, ValueError):
        logger.warning("browser realtime message failed")
        return False
    if claims.workspace_id is None:
        await _reject(websocket, "missing workspace")
        return False
    browser_hub.record_principal(websocket, claims.workspace_id, claims.user_id)
    logger.info(
        "browser realtime authenticated workspaceId=%s userId=%s",
        claims.workspace_id,
        claims.user_id,
    )
    return True


async def _subscribe(websocket: WebSocket, payload: dict[str, object]) -> None:
    channel = payload.get("channel")
    if not isinstance(channel, str) or channel.strip() == "":
        return
    principal = browser_hub.principal(websocket)
    if principal is None:
        return
    allowed = await _authorize(principal.tenant_id, principal.user_id, channel)
    if not allowed:
        logger.warning(
            "browser subscription denied workspaceId=%s userId=%s channel=%s",
            principal.tenant_id,
            principal.user_id,
            channel,
        )
        return
    browser_hub.add_subscription(websocket, channel)


def _unsubscribe(websocket: WebSocket, payload: dict[str, object]) -> None:
    channel = payload.get("channel")
    if not isinstance(channel, str) or channel.strip() == "":
        return
    browser_hub.remove_subscription(websocket, channel)


async def _authorize(workspace_id: int, user_id: int, channel: str) -> bool:
    if channel.startswith(DISPATCH_PREFIX):
        return await _authorize_dispatch(workspace_id, user_id, channel)
    if channel.startswith(CONVERSATION_PREFIX):
        return await _authorize_conversation(workspace_id, channel)
    if channel.startswith(SCHEDULED_RUN_PREFIX):
        return await _authorize_scheduled_run(workspace_id, user_id, channel)
    return False


async def _authorize_dispatch(workspace_id: int, user_id: int, channel: str) -> bool:
    if workspace_id <= 0 or user_id <= 0:
        return False
    dispatch_id = _suffix_id(channel, DISPATCH_PREFIX)
    if dispatch_id is None or dispatch_id <= 0:
        return False
    async with SessionLocal() as session:
        dispatch = await session.scalar(
            select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
        )
        if dispatch is None or dispatch.tenant_id != workspace_id:
            return False
        member = await _active_member(session, workspace_id, user_id)
        if member is None:
            return False
        return _allows_read(member.access_level)


async def _authorize_conversation(workspace_id: int, channel: str) -> bool:
    conversation_id = _suffix_id(channel, CONVERSATION_PREFIX)
    if conversation_id is None:
        return False
    async with SessionLocal() as session:
        conversation = await session.scalar(
            select(AgentConversation)
            .where(
                AgentConversation.id == conversation_id,
                AgentConversation.tenant_id == workspace_id,
            )
            .limit(1)
        )
    if conversation is None:
        logger.warning(
            "conversation subscription denied: not found workspaceId=%s conversationId=%s",
            workspace_id,
            conversation_id,
        )
        return False
    return True


async def _authorize_scheduled_run(workspace_id: int, user_id: int, channel: str) -> bool:
    if workspace_id <= 0 or user_id <= 0:
        return False
    run_id = _suffix_id(channel, SCHEDULED_RUN_PREFIX)
    if run_id is None or run_id <= 0:
        return False
    require_scheduled_capability()
    async with SessionLocal() as session:
        run = await session.scalar(
            select(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.id == run_id,
                ScheduledTaskRun.workspace_id == workspace_id,
            )
            .limit(1)
        )
        member = await _active_member(session, workspace_id, user_id)
        if run is None or member is None:
            return False
        return _allows_read(member.access_level)


async def _active_member(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
) -> OrgMember | None:
    """正常成员。状态 0 与表注释和 ``WorkspaceService`` 一致。"""
    return await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user_id,
            OrgMember.status == MEMBER_STATUS_ACTIVE,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )


def _allows_read(level: str) -> bool:
    try:
        return WorkspaceAccessLevel[level].allows(WorkspaceAccessLevel.READ_ONLY)
    except KeyError:
        return False


def _suffix_id(channel: str, prefix: str) -> int | None:
    try:
        return int(channel[len(prefix) :])
    except ValueError:
        return None


async def _reject(websocket: WebSocket, reason: str) -> None:
    browser_hub.remove_session(websocket)
    try:
        await websocket.close(code=VIOLATED_POLICY_CLOSE_CODE, reason=reason)
    except Exception:
        return
