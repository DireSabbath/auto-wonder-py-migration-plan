"""本节点上的执行器会话表。同一执行器新连接会关掉旧连接。"""

import asyncio
import logging
from collections.abc import Mapping

from starlette.websockets import WebSocket, WebSocketState

from autowonder.ws.frames import EXECUTOR_REPLACED_CLOSE_CODE, EXECUTOR_REPLACED_REASON

logger = logging.getLogger(__name__)


class ExecutorSession:
    """一条已鉴权的执行器连接。出站帧按连接加锁，避免两帧交错。"""

    def __init__(
        self,
        executor_id: int,
        agent_id: int,
        tenant_id: int,
        max_concurrent_dispatches: int,
        session_id: str,
        websocket: WebSocket,
    ) -> None:
        self.executor_id = executor_id
        self.agent_id = agent_id
        self.tenant_id = tenant_id
        self.max_concurrent_dispatches = max_concurrent_dispatches
        self.session_id = session_id
        self.websocket = websocket
        self._send_lock = asyncio.Lock()
        self._replacement_recovery_pending = False

    def is_open(self) -> bool:
        """连接是否仍可写。"""
        return self.websocket.client_state == WebSocketState.CONNECTED

    async def send_text(self, message: str) -> None:
        """发送一帧文本。同一会话上的发送串行。"""
        async with self._send_lock:
            await self.websocket.send_text(message)

    def mark_replacement_recovery_pending(self) -> None:
        """新登记的连接要在下一次心跳补回被替换前中断的会话。"""
        self._replacement_recovery_pending = True

    def consume_replacement_recovery_pending(self) -> bool:
        """第一次心跳消费替换标记，之后的心跳不再重复补回。"""
        pending = self._replacement_recovery_pending
        self._replacement_recovery_pending = False
        return pending


class SessionRegistry:
    """executorId → 当前连接，以及 sessionId → executorId。"""

    def __init__(self) -> None:
        self._by_executor: dict[int, ExecutorSession] = {}
        self._by_session: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def register(self, session: ExecutorSession) -> None:
        """登记连接。同一执行器已有连接时先从表里摘掉，再以 4001 关闭旧连接。"""
        session.mark_replacement_recovery_pending()
        replaced: ExecutorSession | None = None
        async with self._lock:
            previous = self._by_executor.get(session.executor_id)
            self._by_executor[session.executor_id] = session
            if previous is not None and previous is not session:
                self._by_session.pop(previous.session_id, None)
                replaced = previous
            self._by_session[session.session_id] = session.executor_id
        logger.info(
            "session register executorId=%s sessionId=%s",
            session.executor_id,
            session.session_id,
        )
        if replaced is not None:
            logger.info(
                "session replaced executorId=%s oldSessionId=%s",
                session.executor_id,
                replaced.session_id,
            )
            await _close_replaced(replaced)

    def find_by_executor_id(self, executor_id: int) -> ExecutorSession | None:
        """当前登记的连接。没有时返回 None。"""
        return self._by_executor.get(executor_id)

    def is_current(self, session: ExecutorSession) -> bool:
        """这是不是该执行器表里的那一条，被替换后的旧连接不是。"""
        return self._by_executor.get(session.executor_id) is session

    async def remove_by_session_id(self, session_id: str) -> ExecutorSession | None:
        """只删除仍是当前连接的会话。已被替换的旧 id 返回 None。"""
        async with self._lock:
            executor_id = self._by_session.pop(session_id, None)
            if executor_id is None:
                return None
            current = self._by_executor.get(executor_id)
            if current is not None and current.session_id == session_id:
                del self._by_executor[executor_id]
                logger.info(
                    "session removed executorId=%s sessionId=%s",
                    executor_id,
                    session_id,
                )
                return current
            return None


async def _close_replaced(replaced: ExecutorSession) -> None:
    try:
        await replaced.websocket.close(
            code=EXECUTOR_REPLACED_CLOSE_CODE,
            reason=EXECUTOR_REPLACED_REASON,
        )
    except RuntimeError:
        logger.warning(
            "failed to close replaced executor session executorId=%s sessionId=%s",
            replaced.executor_id,
            replaced.session_id,
        )


session_registry = SessionRegistry()


def client_ip(headers: Mapping[str, str]) -> str | None:
    """从握手头解析客户端 IP。优先 ``x-forwarded-for``，跳过 unknown，最长 64。"""
    forwarded = headers.get("x-forwarded-for")
    ip = _first_hop(forwarded)
    if ip is None:
        ip = _first_hop(headers.get("x-real-ip"))
    if ip is None:
        return None
    if len(ip) > 64:
        return ip[:64]
    return ip


def _first_hop(value: str | None) -> str | None:
    if value is None:
        return None
    for part in value.split(","):
        trimmed = part.strip()
        if trimmed != "" and trimmed.lower() != "unknown":
            return trimmed
    return None
