"""把 MCP 连接测试下发给在线 Runtime，并在 Redis 上收回结果。"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.redis import redis_client
from autowonder.executors.models import Executor
from autowonder.ws.frames import BROADCAST_CHANNEL
from autowonder.ws.session import session_registry

logger = logging.getLogger(__name__)

RESULT_TTL_SECONDS = 120
MIN_WAIT_MILLIS = 90_000
MAX_WAIT_MILLIS = 615_000


@dataclass(frozen=True)
class SkillConnectionTestResult:
    """Runtime 回传的一次连接测试。"""

    success: bool
    message: str | None
    duration_ms: int | None
    tools: list[dict[str, Any]]


def wait_millis(timeout_seconds: int) -> int:
    """等待上限夹在 90 秒和 615 秒之间，并在超时时间外再留 15 秒。"""
    return min(MAX_WAIT_MILLIS, max(MIN_WAIT_MILLIS, timeout_seconds * 1_000 + 15_000))


def ticket_key(test_id: str) -> str:
    """这次测试属于哪个工作空间和执行器。"""
    return "mcp:connection:test:ticket:" + test_id


def result_v2_key(test_id: str) -> str:
    """当前结果键。值是 JSON。"""
    return "mcp:connection:test:result:v2:" + test_id


def result_key(test_id: str) -> str:
    """滚动发布期间的旧结果键。"""
    return "mcp:connection:test:result:" + test_id


def tools_key(test_id: str) -> str:
    """旧结果配套的工具列表键。"""
    return "mcp:connection:test:tools:" + test_id


def decode_v2_result(test_id: str, encoded_result: str) -> SkillConnectionTestResult:
    """解析 V2 JSON。格式不对时返回可展示的失败，不把原文抛出去。"""
    try:
        result = json.loads(encoded_result)
        tools = _tools(result.get("tools"))
        duration = result.get("durationMs")
        duration_ms = None
        if duration is not None:
            duration_ms = int(duration)
        return SkillConnectionTestResult(
            result.get("success") is True,
            result.get("message"),
            duration_ms,
            tools,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.error(
            "MCP connection test V2 result is invalid testId=%s redisKey=%s",
            test_id,
            result_v2_key(test_id),
        )
        return SkillConnectionTestResult(False, "测试结果格式无效，请重新测试", None, [])


async def test_runtime(
    session: AsyncSession,
    tenant_id: int,
    executor_id: int,
    transport: str,
    command: str | None,
    args: list[str],
    url: str | None,
    headers: dict[str, str],
    env: dict[str, str],
    timeout_seconds: int,
) -> SkillConnectionTestResult:
    """向指定执行器下发测试帧，并等到结果或超时。"""
    executor = await session.scalar(
        select(Executor)
        .where(Executor.id == executor_id, Executor.is_deleted == 0)
        .limit(1)
    )
    if executor is None or executor.tenant_id != tenant_id:
        raise BizError(ErrorCode.EXECUTOR_NOT_FOUND)
    test_id = str(uuid.uuid4())
    client = redis_client()
    stored = await client.set(
        ticket_key(test_id),
        json.dumps({"tenantId": tenant_id, "executorId": executor_id}, separators=(",", ":")),
        ex=RESULT_TTL_SECONDS,
    )
    if stored is not True:
        raise RuntimeError("无法创建 Runtime 测试请求")
    frame = {
        "type": "MCP_CONNECTION_TEST",
        "testId": test_id,
        "executorId": executor_id,
        "transport": transport,
        "command": command,
        "args": args,
        "url": url,
        "headers": headers,
        "env": env,
        "timeoutSeconds": timeout_seconds,
    }
    await _send(executor_id, json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
    deadline = time.monotonic() + wait_millis(timeout_seconds) / 1000
    while time.monotonic() < deadline:
        encoded = await client.get(result_v2_key(test_id))
        if encoded is not None:
            return decode_v2_result(test_id, encoded)
        try:
            legacy = await client.get(result_key(test_id))
        except Exception:
            logger.error(
                "MCP connection test legacy result is incompatible testId=%s "
                "redisKey=%s tenantId=%s executorId=%s",
                test_id,
                result_key(test_id),
                tenant_id,
                executor_id,
            )
            return SkillConnectionTestResult(
                False, "测试结果与灰度版本不兼容，请稍后重新测试", None, []
            )
        if legacy is not None:
            return SkillConnectionTestResult(
                False, "测试结果与灰度版本不兼容，请稍后重新测试", None, []
            )
        await asyncio.sleep(0.05)
    seconds = wait_millis(timeout_seconds) // 1000
    return SkillConnectionTestResult(
        False,
        f"Runtime 未在 {seconds} 秒内返回；请确认该 Runtime 在线且已更新",
        None,
        [],
    )


async def complete_connection_test(
    tenant_id: int,
    executor_id: int,
    test_id: str | None,
    success: bool,
    message: str | None,
    duration_ms: int | None,
    tools: list[dict[str, Any]],
) -> None:
    """执行器回报测试结果。票据不属于这次调用时直接丢掉。"""
    if test_id is None:
        return
    client = redis_client()
    raw = await client.get(ticket_key(test_id))
    if raw is None:
        return
    ticket = json.loads(raw)
    if ticket.get("tenantId") != tenant_id or ticket.get("executorId") != executor_id:
        return
    body = {
        "success": success,
        "message": message,
        "durationMs": duration_ms,
        "tools": tools,
    }
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    await client.set(result_v2_key(test_id), encoded, ex=RESULT_TTL_SECONDS)


async def _send(executor_id: int, payload: str) -> None:
    try:
        current = session_registry.find_by_executor_id(executor_id)
        if current is not None and current.is_open():
            await current.send_text(payload)
            return
        await redis_client().publish(BROADCAST_CHANNEL, payload)
    except Exception as error:
        raise RuntimeError("Runtime MCP 测试下发失败") from error


def _tools(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    tools: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            tools.append({str(key): item[key] for key in item})
    return tools
