"""会话运行时帧。字段顺序对齐 ``WsConversationTransport``。"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.config import get_settings
from autowonder.conversations.constants import PLATFORM_CHANNEL
from autowonder.conversations.models import AgentConversation
from autowonder.conversations.records import find_agent
from autowonder.core.errors import IllegalArgumentError
from autowonder.dispatch.selector import ProtocolCompatibilityError
from autowonder.environments.snapshot import resolve_snapshot
from autowonder.mcp.conversation_tokens import issue_conversation_token
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.storage.factory import resolve_bucket
from autowonder.storage.objects import get_object_storage
from autowonder.taskpackages.assembler import _capabilities, _repo_map, _repos
from autowonder.taskpackages.packager import (
    TaskPackager,
    normalize_base_url,
    normalize_mcp_url,
)
from autowonder.ws.frames import AGENT_ENVIRONMENT_VARIABLES_V1
from autowonder.ws.mailbox import deliver_executor_frame
from autowonder.ws.presence import presence_manager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapabilitySnapshot:
    """一轮会话下发需要的能力包与 MCP 身份。"""

    agent_version_id: int
    download_url: str
    sha256: str
    capability_hash: str
    mcp_token: str
    mcp_secrets: dict[str, str]


def turn_frame(
    *,
    executor_id: int,
    conversation_id: int,
    turn_id: int,
    agent_id: int,
    dispatch_attempt: int | None,
    content: str | None,
    request_id: str,
    cli_session_ref: str,
    system_prompt: str,
    agent_version_id: int,
    download_url: str,
    sha256: str,
    capability_hash: str,
    mcp_token: str,
    mcp_secrets: dict[str, str],
    environment_variables: dict[str, str],
) -> dict[str, object]:
    """``CONVERSATION_TURN``。空环境变量仍写出，避免执行器把缺键当成未协商。"""
    frame: dict[str, object] = {
        "type": "CONVERSATION_TURN",
        "executorId": executor_id,
        "conversationId": conversation_id,
        "turnId": turn_id,
        "agentId": agent_id,
    }
    if dispatch_attempt is not None:
        frame["dispatchAttempt"] = dispatch_attempt
    frame["content"] = content
    frame["requestId"] = request_id
    frame["cliSessionRef"] = cli_session_ref
    frame["systemPrompt"] = system_prompt
    frame["agentVersionId"] = agent_version_id
    frame["capabilityDownloadUrl"] = download_url
    frame["capabilitySha256"] = sha256
    frame["capabilityHash"] = capability_hash
    frame["mcpToken"] = mcp_token
    frame["mcpSecrets"] = mcp_secrets
    frame["environmentVariables"] = environment_variables
    return frame


def cancel_frame(executor_id: int, conversation_id: int, turn_id: int) -> dict[str, object]:
    """``CONVERSATION_TURN_CANCEL``。"""
    return {
        "type": "CONVERSATION_TURN_CANCEL",
        "executorId": executor_id,
        "conversationId": conversation_id,
        "turnId": turn_id,
    }


def elicitation_reply_frame(
    executor_id: int,
    conversation_id: int,
    turn_id: int,
    request_id: str,
    action: str | None,
    answer_json: str | None,
) -> dict[str, object]:
    """``CONVERSATION_ELICITATION_REPLY``。空白答案不放 ``content``。"""
    frame: dict[str, object] = {
        "type": "CONVERSATION_ELICITATION_REPLY",
        "executorId": executor_id,
        "conversationId": conversation_id,
        "turnId": turn_id,
        "requestId": request_id,
        "action": action,
    }
    if answer_json is not None and answer_json.strip() != "":
        parsed = json.loads(answer_json)
        if not isinstance(parsed, dict):
            raise ValueError("elicitation answer must be a JSON object")
        frame["content"] = parsed
    return frame


def commands_probe_frame(executor_id: int, conversation_id: int) -> dict[str, object]:
    """``CONVERSATION_COMMANDS_PROBE``。工作目录由执行器自己派生。"""
    return {
        "type": "CONVERSATION_COMMANDS_PROBE",
        "executorId": executor_id,
        "conversationId": conversation_id,
    }


def dump_conversation_frame(frame: dict[str, object]) -> str:
    """紧凑 JSON。空对象保持 ``{}``，null 仍写出。"""
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def resolve_mcp_principal(conversation: AgentConversation, agent: Agent) -> int:
    """平台管家只用会话主人。其他渠道用创建者，没有再退到修改者。"""
    if conversation.channel == PLATFORM_CHANNEL:
        owner = conversation.owner_user_id
        if owner is None or owner <= 0:
            raise RuntimeError("platform conversation owner is unavailable")
        return owner
    principal_id = 0 if agent.creator_id is None else agent.creator_id
    if principal_id <= 0 and agent.modifier_id is not None:
        principal_id = agent.modifier_id
    if principal_id <= 0:
        raise RuntimeError("conversation MCP principal is unavailable")
    return principal_id


def resolve_mcp_secrets(refs: dict[str, str]) -> dict[str, str]:
    """没有密文引用时不碰主密钥。键就是密文本身。"""
    if len(refs) == 0:
        return {}
    crypto = AesGcmSecretCrypto(get_settings().secret_master_key)
    values: dict[str, str] = {}
    for ref in refs:
        values[ref] = crypto.decrypt(ref)
    return values


def _request_id(request_id: str | None) -> str:
    if request_id is None or request_id == "":
        return ""
    return request_id


def _text(value: str | None) -> str:
    if value is None:
        return ""
    return value


def _bound_executor(conversation: AgentConversation) -> int:
    executor_id = conversation.executor_id
    if executor_id is None:
        raise IllegalArgumentError("conversation must have a bound executor")
    return executor_id


async def prepare_capability(
    session: AsyncSession,
    conversation: AgentConversation,
    turn_id: int,
) -> CapabilitySnapshot:
    """按冻结版本打包会话能力，并签给这一轮的 MCP 主体。"""
    version_id = conversation.agent_version_id
    if version_id is None:
        raise IllegalArgumentError("conversation capability identity is incomplete")
    agent = await find_agent(session, conversation.agent_id)
    if agent is None or agent.tenant_id != conversation.tenant_id:
        raise RuntimeError("conversation agent is unavailable")
    principal_id = resolve_mcp_principal(conversation, agent)
    repos = await _repos(session, conversation.tenant_id, version_id, agent)
    capabilities = await _capabilities(session, conversation.tenant_id, version_id)
    repo_map = await _repo_map(session, conversation.tenant_id, repos)
    bundle = _packager().build_conversation_capabilities(
        conversation.tenant_id,
        conversation.id,
        turn_id,
        conversation.agent_id,
        version_id,
        capabilities,
        repos,
        repo_map,
    )
    token = issue_conversation_token(conversation, principal_id)
    return CapabilitySnapshot(
        version_id,
        bundle.download_url,
        bundle.sha256,
        bundle.content_hash,
        token,
        resolve_mcp_secrets(bundle.mcp_secret_refs),
    )


async def send_turn(
    session: AsyncSession,
    conversation: AgentConversation,
    turn_id: int,
    content: str | None,
    system_prompt: str | None,
    dispatch_attempt: int | None,
    request_id: str | None,
) -> None:
    """打包能力与环境变量后，把这一轮交给绑定的执行器。"""
    executor_id = _bound_executor(conversation)
    logger.info(
        "conversation turn dispatch conversationId=%s turnId=%s executorId=%s attempt=%s",
        conversation.id,
        turn_id,
        executor_id,
        dispatch_attempt,
    )
    capability = await prepare_capability(session, conversation, turn_id)
    environment = dict(
        await resolve_snapshot(session, conversation.tenant_id, capability.agent_version_id)
    )
    await _require_environment(executor_id, environment)
    payload = dump_conversation_frame(
        turn_frame(
            executor_id=executor_id,
            conversation_id=conversation.id,
            turn_id=turn_id,
            agent_id=conversation.agent_id,
            dispatch_attempt=dispatch_attempt,
            content=content,
            request_id=_request_id(request_id),
            cli_session_ref=_text(conversation.cli_session_ref),
            system_prompt=_text(system_prompt),
            agent_version_id=capability.agent_version_id,
            download_url=capability.download_url,
            sha256=capability.sha256,
            capability_hash=capability.capability_hash,
            mcp_token=capability.mcp_token,
            mcp_secrets=capability.mcp_secrets,
            environment_variables=environment,
        )
    )
    await deliver_conversation(executor_id, payload)


async def send_cancel(conversation: AgentConversation, turn_id: int) -> None:
    """通知执行器停止生成这一轮。"""
    executor_id = _bound_executor(conversation)
    await deliver_conversation(
        executor_id,
        dump_conversation_frame(cancel_frame(executor_id, conversation.id, turn_id)),
    )


async def send_elicitation_reply(
    conversation: AgentConversation,
    turn_id: int,
    request_id: str,
    action: str | None,
    answer_json: str | None,
) -> None:
    """把问答卡片的动作送回执行器。decline 与 cancel 不带答案。"""
    executor_id = _bound_executor(conversation)
    payload = dump_conversation_frame(
        elicitation_reply_frame(
            executor_id,
            conversation.id,
            turn_id,
            request_id,
            action,
            answer_json,
        )
    )
    await deliver_conversation(executor_id, payload)


async def send_commands_probe(conversation: AgentConversation) -> None:
    """向绑定执行器要斜杠命令。"""
    executor_id = _bound_executor(conversation)
    await deliver_conversation(
        executor_id,
        dump_conversation_frame(commands_probe_frame(executor_id, conversation.id)),
    )


async def deliver_conversation(executor_id: int, payload: str) -> None:
    """本机直发或广播。失败统一成会话传输错误，避免把调度文案漏给轮次。"""
    try:
        await deliver_executor_frame(executor_id, payload)
    except Exception as error:
        raise RuntimeError("WebSocket conversation send failed") from error


async def _require_environment(executor_id: int, variables: Mapping[str, str]) -> None:
    if len(variables) == 0:
        return
    supported = await presence_manager.supports_protocol_feature(
        executor_id,
        AGENT_ENVIRONMENT_VARIABLES_V1,
    )
    if not supported:
        raise ProtocolCompatibilityError(AGENT_ENVIRONMENT_VARIABLES_V1)


def _packager() -> TaskPackager:
    settings = get_settings()
    bucket = resolve_bucket(settings.oss_task_pkg_bucket, settings.oss_bucket)
    mcp_url = normalize_mcp_url(normalize_base_url(settings.public_base_url) + "/api/mcp")
    return TaskPackager(get_object_storage(), bucket, mcp_url)
