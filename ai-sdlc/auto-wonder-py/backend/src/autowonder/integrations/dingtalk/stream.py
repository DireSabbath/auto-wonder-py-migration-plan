"""钉钉 Stream 长连接。只订阅机器人消息，并把文本交给会话入站。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote_plus

import httpx
from sqlalchemy import update
from websockets.asyncio.client import connect as connect_websocket

from autowonder.config import get_settings
from autowonder.core.clock import now_local
from autowonder.db.session import SessionLocal
from autowonder.integrations.dingtalk.sender import resolve_base_url
from autowonder.integrations.models import DingtalkRobotBinding
from autowonder.security.crypto import AesGcmSecretCrypto

logger = logging.getLogger(__name__)

BOT_TOPIC = "/v1.0/im/bot/messages/get"
CREDENTIAL_UNREADABLE = "DingTalk Stream credential is unreadable"
CONNECTION_FAILED = "DingTalk Stream connection failed"
_BACKOFF_MAX = 30.0

StreamWriter = Callable[[int, str, str | None], Awaitable[None]]

_TASKS: dict[int, asyncio.Task[None]] = {}
_GUARDS: dict[int, asyncio.Lock] = {}
_STOP: set[int] = set()


def open_connection_body(client_id: str, client_secret: str) -> dict[str, object]:
    """只订阅机器人下行，不订阅事件通配。"""
    return {
        "clientId": client_id,
        "clientSecret": client_secret,
        "subscriptions": [{"type": "CALLBACK", "topic": BOT_TOPIC}],
        "ua": "autowonder-python",
        "localIp": "127.0.0.1",
    }


def frame_kind(frame: dict[str, object]) -> str:
    """把一帧分成心跳、断开、机器人消息或忽略。"""
    kind = frame.get("type")
    topic = _headers(frame).get("topic")
    if kind == "SYSTEM" and topic == "ping":
        return "ping"
    if kind == "SYSTEM" and topic == "disconnect":
        return "disconnect"
    if kind == "CALLBACK" and topic == BOT_TOPIC:
        return "bot"
    return "ignore"


def system_ack(frame: dict[str, object]) -> dict[str, object]:
    """心跳和断开回执。data 带回服务端原文。"""
    headers = _headers(frame)
    return {
        "code": 200,
        "headers": {
            "contentType": "application/json",
            "messageId": headers.get("messageId"),
        },
        "message": "OK",
        "data": _echo_data(frame.get("data")),
    }


def callback_ack(frame: dict[str, object], code: int) -> dict[str, object]:
    """机器人回调回执。data 是 JSON 文本。"""
    message = "OK"
    if code != 200:
        message = "ERROR"
    headers = _headers(frame)
    return {
        "code": code,
        "headers": {
            "contentType": "application/json",
            "messageId": headers.get("messageId"),
        },
        "message": message,
        "data": json.dumps({"response": message}),
    }


def inbound_text(data: object) -> tuple[str, str, str] | None:
    """只接受带 msgId、会话和正文的文本消息。"""
    payload = _dict_payload(data)
    if payload is None or payload.get("msgtype") != "text":
        return None
    msg_id = payload.get("msgId")
    conversation_id = payload.get("conversationId")
    text = payload.get("text")
    content = None
    if isinstance(text, dict):
        content = text.get("content")
    if not isinstance(msg_id, str) or msg_id.strip() == "":
        return None
    if not isinstance(conversation_id, str) or conversation_id.strip() == "":
        return None
    if not isinstance(content, str) or content.strip() == "":
        return None
    return msg_id, conversation_id, content.strip()


async def ensure_started(row: DingtalkRobotBinding, write: StreamWriter) -> None:
    """已有连接时直接返回。凭据读不出时写成 FAILED 并抛出原异常。"""
    async with _guard(row.id):
        current = _TASKS.get(row.id)
        if current is not None and not current.done():
            return
        _STOP.discard(row.id)
        await write(row.id, "CONNECTING", None)
        try:
            secret = _decrypt(row.credential_ref)
        except Exception:
            await write(row.id, "FAILED", CREDENTIAL_UNREADABLE)
            raise
        _TASKS[row.id] = asyncio.create_task(
            _run(
                row.id,
                row.tenant_id,
                row.agent_id,
                row.app_key,
                secret,
                row.base_url,
                write,
            )
        )


async def ensure_stopped(row: DingtalkRobotBinding, write: StreamWriter) -> None:
    """取消进程内连接，并把状态写成未连接。"""
    _STOP.add(row.id)
    async with _guard(row.id):
        task = _TASKS.pop(row.id, None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await write(row.id, "NOT_CONNECTED", None)


def _decrypt(credential_ref: str) -> str:
    return AesGcmSecretCrypto(get_settings().secret_master_key).decrypt(credential_ref)


def _guard(binding_id: int) -> asyncio.Lock:
    lock = _GUARDS.get(binding_id)
    if lock is None:
        lock = asyncio.Lock()
        _GUARDS[binding_id] = lock
    return lock


def _headers(frame: dict[str, object]) -> dict[str, object]:
    headers = frame.get("headers")
    if isinstance(headers, dict):
        return headers
    return {}


def _echo_data(data: object) -> str:
    echoed = data
    if isinstance(data, str) and data.strip() != "":
        try:
            echoed = json.loads(data)
        except json.JSONDecodeError:
            echoed = data
    return json.dumps(echoed)


def _dict_payload(data: object) -> dict[str, object] | None:
    if isinstance(data, dict):
        return data
    if not isinstance(data, str) or data.strip() == "":
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


async def _run(
    binding_id: int,
    tenant_id: int,
    agent_id: int,
    app_key: str,
    secret: str,
    base_url: str | None,
    write: StreamWriter,
) -> None:
    delay = 1.0
    try:
        while binding_id not in _STOP:
            try:
                await _listen(binding_id, tenant_id, agent_id, app_key, secret, base_url, write)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if binding_id in _STOP:
                    break
                logger.warning(
                    "DingTalk Stream connection failed bindingId=%s errorType=%s",
                    binding_id,
                    type(error).__name__,
                )
                await write(binding_id, "FAILED", CONNECTION_FAILED)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_MAX)
    finally:
        _TASKS.pop(binding_id, None)


async def _listen(
    binding_id: int,
    tenant_id: int,
    agent_id: int,
    app_key: str,
    secret: str,
    base_url: str | None,
    write: StreamWriter,
) -> None:
    endpoint, ticket = await _open_connection(base_url, app_key, secret)
    uri = endpoint + "?ticket=" + quote_plus(ticket)
    async with connect_websocket(uri, open_timeout=10) as socket:
        await write(binding_id, "CONNECTED", None)
        async for raw in socket:
            if binding_id in _STOP:
                return
            try:
                leave = await _on_raw(socket, binding_id, tenant_id, agent_id, raw)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("DingTalk Stream frame failed bindingId=%s", binding_id)
                continue
            if leave:
                return


async def _open_connection(
    base_url: str | None,
    app_key: str,
    secret: str,
) -> tuple[str, str]:
    url = resolve_base_url(base_url) + "/v1.0/gateway/connections/open"
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        response = await client.post(
            url,
            json=open_connection_body(app_key, secret),
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        parsed = response.json()
    return str(parsed["endpoint"]), str(parsed["ticket"])


async def _on_raw(
    socket: Any,
    binding_id: int,
    tenant_id: int,
    agent_id: int,
    raw: str | bytes,
) -> bool:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return False
    kind = frame_kind(parsed)
    if kind == "ping":
        await _send(socket, system_ack(parsed))
        return False
    if kind == "disconnect":
        await _send(socket, system_ack(parsed))
        return True
    if kind == "bot":
        await _accept_bot(socket, binding_id, tenant_id, agent_id, parsed)
    return False


async def _accept_bot(
    socket: Any,
    binding_id: int,
    tenant_id: int,
    agent_id: int,
    frame: dict[str, object],
) -> None:
    code = 200
    text = inbound_text(frame.get("data"))
    if text is not None:
        msg_id, conversation_id, content = text
        try:
            await _store_text(binding_id, tenant_id, agent_id, msg_id, conversation_id, content)
        except Exception:
            code = 500
            logger.warning("DingTalk Stream inbound failed bindingId=%s", binding_id)
    await _send(socket, callback_ack(frame, code))


async def _store_text(
    binding_id: int,
    tenant_id: int,
    agent_id: int,
    msg_id: str,
    conversation_id: str,
    content: str,
) -> None:
    from autowonder.conversations.turns import submit_inbound

    async with SessionLocal() as session:
        await submit_inbound(
            session,
            tenant_id,
            agent_id,
            "DINGTALK",
            str(binding_id) + ":" + conversation_id,
            content,
            "DINGTALK:" + str(binding_id) + ":" + msg_id,
        )
        await session.execute(
            update(DingtalkRobotBinding)
            .where(
                DingtalkRobotBinding.id == binding_id,
                DingtalkRobotBinding.tenant_id == tenant_id,
            )
            .values(last_success_at=now_local(), last_error=None)
        )
        await session.commit()


async def _send(socket: Any, payload: dict[str, object]) -> None:
    await socket.send(json.dumps(payload))
