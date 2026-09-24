"""斜杠命令快照。Redis 不可用时详情仍返回，只是没有命令补全。"""

import json
import logging
from collections.abc import Awaitable
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.conversations.constants import (
    PROBE_LOCK_PREFIX,
    PROBE_LOCK_TTL_SEC,
    SNAPSHOT_KEY_PREFIX,
)
from autowonder.conversations.records import find_conversation
from autowonder.conversations.schemas import SlashCommandInput, SlashCommandView
from autowonder.core.redis import redis_client

logger = logging.getLogger(__name__)


def commands_from_snapshot(raw: str | None) -> list[SlashCommandView]:
    """解析上次探针留下的快照。空文本或坏 JSON 视为没有命令。"""
    if raw is None or raw.strip() == "":
        return []
    try:
        payload = _available_commands(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("commands snapshot unparsable")
        return []
    return [_command(item) for item in payload]


async def snapshot(tenant_id: int, conversation_id: int) -> list[SlashCommandView]:
    """读命令快照。Redis 故障时返回空列表，不挡住会话详情。"""
    try:
        raw = await cast(
            Awaitable[str | None],
            redis_client().get(SNAPSHOT_KEY_PREFIX + str(conversation_id)),
        )
    except Exception:
        logger.warning(
            "commands snapshot read degraded tenantId=%s conversationId=%s",
            tenant_id,
            conversation_id,
        )
        return []
    return commands_from_snapshot(raw)


async def refresh(session: AsyncSession, tenant_id: int, conversation_id: int) -> None:
    """打开会话时尝试发一次命令探针。没绑定执行器或锁被占用就跳过。"""
    conversation = await find_conversation(session, tenant_id, conversation_id)
    if conversation is None or conversation.executor_id is None:
        logger.info(
            "commands refresh skipped: no bound executor conversationId=%s",
            conversation_id,
        )
        return
    try:
        acquired = await cast(
            Awaitable[bool | None],
            redis_client().set(
                PROBE_LOCK_PREFIX + str(conversation_id),
                "1",
                nx=True,
                ex=PROBE_LOCK_TTL_SEC,
            ),
        )
    except Exception:
        logger.warning("commands refresh lock degraded conversationId=%s", conversation_id)
        return
    if acquired is not True:
        return
    try:
        await send_commands_probe(conversation_id)
    except Exception:
        logger.warning("commands probe dispatch failed conversationId=%s", conversation_id)


async def send_commands_probe(conversation_id: int) -> None:
    """向执行器要斜杠命令。WebSocket 传输尚未迁入。"""
    raise RuntimeError("conversation runtime transport is not available: " + str(conversation_id))


def _available_commands(root: object) -> list[object]:
    if not isinstance(root, dict):
        raise ValueError("snapshot root")
    value = root.get("availableCommands")
    if value is None:
        return []
    if isinstance(value, str):
        if value.strip() == "":
            return []
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("availableCommands")
    return value


def _command(item: object) -> SlashCommandView:
    if not isinstance(item, dict):
        raise ValueError("command")
    hint_source = item.get("input")
    command_input = None
    if isinstance(hint_source, dict):
        hint = hint_source.get("hint")
        command_input = SlashCommandInput(hint=hint if isinstance(hint, str) else None)
    name = item.get("name")
    description = item.get("description")
    return SlashCommandView(
        name=name if isinstance(name, str) else None,
        description=description if isinstance(description, str) else None,
        input=command_input,
    )
