"""把 AI 会话增量、状态和结果发给 Redis 和本机浏览器。"""

import json
import logging
import time
from typing import Any

from autowonder.core.redis import redis_client

logger = logging.getLogger(__name__)


def stream_channel(session_id: int) -> str:
    """执行器侧订阅的 Redis 频道。"""
    return "ai:stream:" + str(session_id)


def frontend_channel(session_id: int) -> str:
    """浏览器订阅的频道。"""
    return "ai:session:" + str(session_id)


async def publish_delta(session_id: int, tenant_id: int, text: str) -> None:
    """推一段模型输出。"""
    await _publish(session_id, tenant_id, {"type": "delta", "sessionId": session_id, "text": text})


async def publish_status(session_id: int, tenant_id: int, status: str) -> None:
    """推会话状态。"""
    logger.info("ai stream publishStatus sessionId=%s status=%s", session_id, status)
    await _publish(
        session_id,
        tenant_id,
        {"type": "status", "sessionId": session_id, "status": status},
    )


async def publish_result(session_id: int, tenant_id: int, result_json: str) -> None:
    """推抽出的 JSON 文本。"""
    logger.info("ai stream publishResult sessionId=%s len=%s", session_id, len(result_json))
    await _publish(
        session_id,
        tenant_id,
        {"type": "result", "sessionId": session_id, "resultJson": result_json},
    )


async def _publish(session_id: int, tenant_id: int, event: dict[str, Any]) -> None:
    from autowonder.ws.browser import deliver_browser_channel

    encoded = json.dumps(event, ensure_ascii=False)
    await redis_client().publish(stream_channel(session_id), encoded)
    channel = frontend_channel(session_id)
    frame = {
        "channel": channel,
        "type": "AI_STREAM",
        "payload": event,
        "timestamp": int(time.time() * 1000),
    }
    logger.info(
        "ai stream browser sessionId=%s tenantId=%s channel=%s",
        session_id,
        tenant_id,
        channel,
    )
    await deliver_browser_channel(channel, json.dumps(frame, ensure_ascii=False))
