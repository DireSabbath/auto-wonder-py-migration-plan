"""跨节点投递。频道是 ``node:dispatch:broadcast``。"""

import json
import logging
from typing import Any

from autowonder.core.redis import redis_client
from autowonder.executors.registry import drop_session
from autowonder.ws.frames import (
    BROADCAST_CHANNEL,
    EXECUTOR_REPLACED_CLOSE_CODE,
    EXECUTOR_REPLACED_REASON,
)
from autowonder.ws.presence import presence_manager
from autowonder.ws.session import session_registry

logger = logging.getLogger(__name__)

CONVERSATION_REDIS_CHANNEL = "autowonder:conversation-events"
SCHEDULED_RUN_PATTERN = "scheduled-run:*"
DISPATCH_PATTERN = "dispatch:*"


async def deliver_executor_frame(executor_id: int, frame_json: str) -> None:
    """本节点有连接就直发，否则发到广播频道，由持有会话的节点转交。"""
    current = session_registry.find_by_executor_id(executor_id)
    if current is not None and current.is_open():
        try:
            await current.send_text(frame_json)
        except Exception as error:
            raise RuntimeError("WebSocket dispatch send failed") from error
        logger.info("dispatch sent local executorId=%s", executor_id)
        return
    try:
        await redis_client().publish(BROADCAST_CHANNEL, frame_json)
    except Exception as error:
        raise RuntimeError("Cross-node dispatch publish failed") from error
    logger.info("dispatch sent remote executorId=%s", executor_id)


async def handle_broadcast(message: str) -> None:
    """处理一封广播：关会话、关掉被替换的连接，或把帧交给本机执行器。"""
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        logger.warning("mailbox delivery failed", exc_info=True)
        return
    if not isinstance(parsed, dict):
        return
    executor_id = _executor_id(parsed)
    if executor_id is None:
        return
    frame_type = parsed.get("type")
    if frame_type == "SESSION_CLOSE":
        await _close_local(executor_id)
        return
    if frame_type == "SESSION_REPLACED":
        await _close_replaced(executor_id)
        return
    logger.info("mailbox broadcast received executorId=%s", executor_id)
    current = session_registry.find_by_executor_id(executor_id)
    if current is None or not current.is_open():
        return
    await current.send_text(message)
    logger.info("mailbox delivered executorId=%s", executor_id)


async def _close_local(executor_id: int) -> None:
    current = session_registry.find_by_executor_id(executor_id)
    if current is None:
        return
    try:
        if current.is_open():
            await current.websocket.close()
            logger.info("session closed via broadcast SESSION_CLOSE executorId=%s", executor_id)
        await presence_manager.unregister(executor_id, current.agent_id)
        drop_session(executor_id)
    except Exception:
        logger.warning(
            "failed to close session via broadcast executorId=%s",
            executor_id,
            exc_info=True,
        )


async def _close_replaced(executor_id: int) -> None:
    current = session_registry.find_by_executor_id(executor_id)
    if current is None:
        return
    current_session_id = await presence_manager.current_session_id(executor_id)
    if current_session_id is None or current_session_id == current.session_id:
        return
    try:
        if current.is_open():
            await current.websocket.close(
                code=EXECUTOR_REPLACED_CLOSE_CODE,
                reason=EXECUTOR_REPLACED_REASON,
            )
            logger.info(
                "replaced session closed via broadcast executorId=%s oldSessionId=%s",
                executor_id,
                current.session_id,
            )
    except Exception:
        logger.warning(
            "failed to close replaced session via broadcast executorId=%s",
            executor_id,
            exc_info=True,
        )


def _executor_id(payload: dict[str, Any]) -> int | None:
    raw = payload.get("executorId")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw
