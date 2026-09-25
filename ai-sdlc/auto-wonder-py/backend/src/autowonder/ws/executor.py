"""执行器 WebSocket。路径 ``/ws/executor``，查询参数 ``executorId`` 与 ``token``。

鉴权失败在握手完成后以关闭码 1008 断开，原因与 Java ``closeQuietly`` 相同。
"""

import logging
import time
import uuid

from fastapi import APIRouter
from sqlalchemy import text, update
from starlette.websockets import WebSocket, WebSocketDisconnect

from autowonder.db.session import SessionLocal
from autowonder.executors.models import Executor
from autowonder.executors.registry import drop_session, register_session
from autowonder.executors.ws_auth import authenticate_executor
from autowonder.ws.frames import VIOLATED_POLICY_CLOSE_CODE
from autowonder.ws.inbound import inbound_router
from autowonder.ws.presence import normalize_capacity, presence_manager
from autowonder.ws.session import ExecutorSession, client_ip, session_registry

logger = logging.getLogger(__name__)

router = APIRouter()
MAX_TEXT_MESSAGE_BYTES = 256 * 1024
HEARTBEAT_THROTTLE_SECONDS = 60
_heartbeat_persisted_at: dict[int, float] = {}


@router.websocket("/ws/executor")
async def executor_socket(
    websocket: WebSocket,
    executorId: str | None = None,
    token: str | None = None,
    maxConcurrentDispatches: str | None = None,
) -> None:
    """接入一个执行器。缺参数、编号非法或令牌不对时关闭连接。"""
    await websocket.accept()
    from autowonder.ws.runtime import ensure_listeners

    ensure_listeners()
    if token is None or executorId is None:
        logger.info("executor auth failed reason=missing_params")
        await _reject(websocket, "missing token or executorId")
        return
    try:
        executor_id = int(executorId)
    except ValueError:
        logger.info("executor auth failed executorId=%s reason=invalid_format", executorId)
        await _reject(websocket, "invalid executorId")
        return
    async with SessionLocal() as session:
        auth = await authenticate_executor(session, executor_id, token)
    if not auth.success:
        logger.info("executor auth failed executorId=%s reason=auth_rejected", executor_id)
        await _reject(websocket, "authentication failed")
        return

    capacity = normalize_capacity(maxConcurrentDispatches)
    executor_session = ExecutorSession(
        auth.executor_id,
        auth.agent_id,
        auth.tenant_id,
        capacity,
        uuid.uuid4().hex,
        websocket,
    )
    await session_registry.register(executor_session)
    tracked = True
    try:
        try:
            announced = await presence_manager.announce_session(
                auth.executor_id,
                executor_session.session_id,
            )
        except Exception:
            await session_registry.remove_by_session_id(executor_session.session_id)
            tracked = False
            logger.warning(
                "executor session initialization failed executorId=%s sessionId=%s",
                auth.executor_id,
                executor_session.session_id,
                exc_info=True,
            )
            raise
        if not announced:
            await session_registry.remove_by_session_id(executor_session.session_id)
            tracked = False
            logger.warning(
                "executor session initialization busy executorId=%s sessionId=%s",
                auth.executor_id,
                executor_session.session_id,
            )
            await _reject(websocket, "session initialization busy")
            return
        register_session(auth.executor_id)
        await _record_connect_ip(websocket, auth.executor_id, auth.tenant_id)
        logger.info(
            "executor connected executorId=%s agentId=%s tenantId=%s capacity=%s",
            auth.executor_id,
            auth.agent_id,
            auth.tenant_id,
            capacity,
        )
        await _read_frames(executor_session)
    finally:
        if tracked:
            removed = await session_registry.remove_by_session_id(executor_session.session_id)
            if removed is not None:
                current = await presence_manager.unregister_if_current(
                    removed.executor_id,
                    removed.agent_id,
                    removed.session_id,
                )
                if current:
                    drop_session(removed.executor_id)
                else:
                    logger.info(
                        "skip presence unregister for replaced session executorId=%s sessionId=%s",
                        removed.executor_id,
                        removed.session_id,
                    )
        logger.info("executor disconnected executorId=%s", auth.executor_id)


async def persist_heartbeat_if_needed(executor_id: int, tenant_id: int) -> None:
    """每 60 秒把 ``last_heartbeat`` 写一次。写库失败不影响这次心跳。"""
    now = time.time()
    last = _heartbeat_persisted_at.get(executor_id)
    if last is not None and now - last < HEARTBEAT_THROTTLE_SECONDS:
        return
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Executor)
                .where(
                    Executor.id == executor_id,
                    Executor.tenant_id == tenant_id,
                    Executor.is_deleted == 0,
                )
                .values(last_heartbeat=text("CURRENT_TIMESTAMP(3)"))
            )
            await session.commit()
        _heartbeat_persisted_at[executor_id] = now
    except Exception:
        logger.warning(
            "failed to persist executor heartbeat executorId=%s tenantId=%s",
            executor_id,
            tenant_id,
            exc_info=True,
        )


async def _read_frames(executor_session: ExecutorSession) -> None:
    while True:
        try:
            message = await executor_session.websocket.receive_text()
        except WebSocketDisconnect:
            return
        if len(message.encode()) > MAX_TEXT_MESSAGE_BYTES:
            logger.warning(
                "ws frame dropped executorId=%s size=%s",
                executor_session.executor_id,
                len(message.encode()),
            )
            continue
        if not session_registry.is_current(executor_session):
            logger.info(
                "ws frame ignored from replaced session executorId=%s sessionId=%s",
                executor_session.executor_id,
                executor_session.session_id,
            )
            continue
        logger.info(
            "ws recv executorId=%s size=%s",
            executor_session.executor_id,
            len(message),
        )
        await _route_safely(executor_session, message)


async def _route_safely(executor_session: ExecutorSession, message: str) -> None:
    try:
        await inbound_router.route(executor_session, message)
    except Exception:
        logger.error(
            "inbound frame handling failed executorId=%s size=%s",
            executor_session.executor_id,
            len(message),
            exc_info=True,
        )


async def _record_connect_ip(websocket: WebSocket, executor_id: int, tenant_id: int) -> None:
    ip = client_ip(websocket.headers)
    if ip is None or ip.strip() == "":
        return
    try:
        async with SessionLocal() as session:
            await session.execute(
                update(Executor)
                .where(
                    Executor.id == executor_id,
                    Executor.tenant_id == tenant_id,
                    Executor.is_deleted == 0,
                )
                .values(last_connect_ip=ip)
            )
            await session.commit()
    except Exception:
        logger.warning(
            "record executor ip failed executorId=%s",
            executor_id,
            exc_info=True,
        )


async def _reject(websocket: WebSocket, reason: str) -> None:
    await websocket.close(code=VIOLATED_POLICY_CLOSE_CODE, reason=reason)
