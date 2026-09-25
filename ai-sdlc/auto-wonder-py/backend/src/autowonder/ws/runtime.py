"""订阅 Redis，把跨节点帧交回本机的执行器或浏览器连接。"""

import asyncio
import json
import logging

from autowonder.core.redis import redis_client
from autowonder.ws.frames import BROADCAST_CHANNEL
from autowonder.ws.mailbox import (
    CONVERSATION_REDIS_CHANNEL,
    DISPATCH_PATTERN,
    SCHEDULED_RUN_PATTERN,
    handle_broadcast,
)

logger = logging.getLogger(__name__)

_task: asyncio.Task[None] | None = None


def ensure_listeners() -> None:
    """第一条连接上来时启动订阅。断线后隔 3 秒重连。"""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_run())


async def _run() -> None:
    while True:
        try:
            await _listen_once()
        except Exception:
            logger.warning("Redis subscriber disconnected, reconnecting in 3s", exc_info=True)
            await asyncio.sleep(3)


async def _listen_once() -> None:
    from autowonder.ws.browser import deliver_browser_channel

    pubsub = redis_client().pubsub()
    await pubsub.subscribe(BROADCAST_CHANNEL, CONVERSATION_REDIS_CHANNEL)
    await pubsub.psubscribe(SCHEDULED_RUN_PATTERN, DISPATCH_PATTERN)
    async for message in pubsub.listen():
        kind = message["type"]
        data = message["data"]
        if not isinstance(data, str):
            continue
        if kind == "message" and message["channel"] == BROADCAST_CHANNEL:
            await handle_broadcast(data)
        elif kind == "message" and message["channel"] == CONVERSATION_REDIS_CHANNEL:
            await _deliver_conversation(data)
        elif kind == "pmessage":
            channel = message["channel"]
            if isinstance(channel, str):
                await deliver_browser_channel(channel, data)


async def _deliver_conversation(message: str) -> None:
    from autowonder.ws.browser import deliver_browser_channel

    try:
        parsed = json.loads(message)
    except json.JSONDecodeError:
        logger.warning("conversation realtime redis delivery failed")
        return
    if not isinstance(parsed, dict):
        return
    channel = parsed.get("channel")
    if isinstance(channel, str):
        await deliver_browser_channel(channel, message)
