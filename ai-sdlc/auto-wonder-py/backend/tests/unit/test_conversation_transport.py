"""会话帧字段顺序、MCP 主体，以及环境变量协议门闩。"""

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.agents.models import Agent
from autowonder.conversations.models import AgentConversation
from autowonder.conversations.runtime_content import runtime_content
from autowonder.conversations.transport import (
    CapabilitySnapshot,
    cancel_frame,
    commands_probe_frame,
    dump_conversation_frame,
    elicitation_reply_frame,
    resolve_mcp_principal,
    resolve_mcp_secrets,
    send_turn,
    turn_frame,
)
from autowonder.core.errors import IllegalArgumentError
from autowonder.dispatch.selector import ProtocolCompatibilityError
from autowonder.ws.frames import AGENT_ENVIRONMENT_VARIABLES_V1


def _conversation() -> AgentConversation:
    return AgentConversation(
        id=4,
        tenant_id=1,
        agent_id=2,
        agent_version_id=3,
        channel="DINGTALK",
        channel_conversation_id="ding",
        executor_id=9,
        cli_session_ref=None,
        status="ACTIVE",
    )


def test_turn_frame_keeps_empty_maps_and_omits_null_attempt() -> None:
    """空环境和空密钥仍是 JSON 对象。没有投递次数时不写该键。"""
    frame = turn_frame(
        executor_id=9,
        conversation_id=4,
        turn_id=8,
        agent_id=2,
        dispatch_attempt=None,
        content=None,
        request_id="",
        cli_session_ref="",
        system_prompt="",
        agent_version_id=3,
        download_url="mem://pkg",
        sha256="abc",
        capability_hash="def",
        mcp_token="tok",
        mcp_secrets={},
        environment_variables={},
    )
    text = dump_conversation_frame(frame)
    assert text == (
        '{"type":"CONVERSATION_TURN","executorId":9,"conversationId":4,"turnId":8,'
        '"agentId":2,"content":null,"requestId":"","cliSessionRef":"","systemPrompt":"",'
        '"agentVersionId":3,"capabilityDownloadUrl":"mem://pkg","capabilitySha256":"abc",'
        '"capabilityHash":"def","mcpToken":"tok","mcpSecrets":{},"environmentVariables":{}}'
    )


def test_cancel_and_probe_frames_carry_executor_id() -> None:
    """跨节点广播靠 executorId 找到本机会话。"""
    assert dump_conversation_frame(cancel_frame(9, 4, 8)) == (
        '{"type":"CONVERSATION_TURN_CANCEL","executorId":9,"conversationId":4,"turnId":8}'
    )
    assert dump_conversation_frame(commands_probe_frame(9, 4)) == (
        '{"type":"CONVERSATION_COMMANDS_PROBE","executorId":9,"conversationId":4}'
    )


def test_elicitation_reply_omits_blank_content_and_keeps_object_order() -> None:
    """decline 没有 content。accept 把答案原文解析成对象，不重排键。"""
    declined = elicitation_reply_frame(9, 4, 8, "req", "decline", None)
    assert "content" not in declined
    accepted = elicitation_reply_frame(9, 4, 8, "req", "accept", '{"b":1,"a":2}')
    assert accepted["content"] == {"b": 1, "a": 2}
    text = dump_conversation_frame(accepted)
    assert '"content":{"b":1,"a":2}' in text


def test_platform_principal_is_the_owner_only() -> None:
    """平台管家没有主人时失败，不退回数字人的创建者。"""
    conversation = _conversation()
    conversation.channel = "PLATFORM_ASSISTANT"
    conversation.owner_user_id = None
    agent = Agent(tenant_id=1, name="chief", creator_id=7, modifier_id=8)
    with pytest.raises(RuntimeError, match="platform conversation owner is unavailable"):
        resolve_mcp_principal(conversation, agent)
    conversation.owner_user_id = 5
    assert resolve_mcp_principal(conversation, agent) == 5


def test_other_channels_fall_back_to_the_modifier() -> None:
    """创建者缺失时用修改者。两个都没有则这一轮不能签 MCP。"""
    conversation = _conversation()
    missing = Agent(tenant_id=1, name="worker", creator_id=None, modifier_id=None)
    with pytest.raises(RuntimeError, match="conversation MCP principal is unavailable"):
        resolve_mcp_principal(conversation, missing)
    edited = Agent(tenant_id=1, name="worker", creator_id=0, modifier_id=8)
    assert resolve_mcp_principal(conversation, edited) == 8


def test_dingtalk_runtime_content_prefixes_the_sender() -> None:
    """钉钉正文带上发送人。没有可用字段或坏 JSON 时仍用原文。"""
    raw = '{"senderNick":"小陈","senderStaffId":"s1","conversationTitle":"群"}'
    text = runtime_content("dingtalk", "你好", raw)
    assert text is not None
    assert text.startswith("DingTalk message context:\n- Sender nickname: 小陈\n")
    assert "\nUser message:\n你好" in text
    assert runtime_content("DINGTALK", "你好", "{}") == "你好"
    assert runtime_content("WORKITEM_CLARIFICATION", "你好", raw) == "你好"
    assert runtime_content("dingtalk", "你好", "{") == "你好"


def test_empty_secret_refs_skip_the_master_key() -> None:
    """没有密文引用时返回空映射。"""
    assert resolve_mcp_secrets({}) == {}


async def test_send_turn_delivers_the_capability_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """能力包和环境变量按字段顺序写进下发帧。"""
    captured: list[str] = []

    async def fake_prepare(
        session: AsyncSession,
        conversation: AgentConversation,
        turn_id: int,
    ) -> CapabilitySnapshot:
        return CapabilitySnapshot(3, "mem://pkg", "abc", "def", "tok", {})

    async def fake_resolve(
        session: AsyncSession,
        tenant_id: int,
        agent_version_id: int,
        decrypt: object = None,
    ) -> dict[str, str]:
        return {}

    async def fake_deliver(executor_id: int, payload: str) -> None:
        captured.append(payload)

    monkeypatch.setattr(
        "autowonder.conversations.transport.prepare_capability",
        fake_prepare,
    )
    monkeypatch.setattr(
        "autowonder.conversations.transport.resolve_snapshot",
        fake_resolve,
    )
    monkeypatch.setattr(
        "autowonder.conversations.transport.deliver_executor_frame",
        fake_deliver,
    )
    await send_turn(
        cast(AsyncSession, object()),
        _conversation(),
        8,
        "hi",
        "sys",
        1,
        "req-1",
        None,
    )
    assert captured == [
        '{"type":"CONVERSATION_TURN","executorId":9,"conversationId":4,"turnId":8,'
        '"agentId":2,"dispatchAttempt":1,"content":"hi","requestId":"req-1",'
        '"cliSessionRef":"","systemPrompt":"sys","agentVersionId":3,'
        '"capabilityDownloadUrl":"mem://pkg","capabilitySha256":"abc",'
        '"capabilityHash":"def","mcpToken":"tok","mcpSecrets":{},"environmentVariables":{}}'
    ]


async def test_send_turn_requires_environment_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非空环境变量而执行器没声明协议时，这一轮不下发。"""

    async def fake_prepare(
        session: AsyncSession,
        conversation: AgentConversation,
        turn_id: int,
    ) -> CapabilitySnapshot:
        return CapabilitySnapshot(3, "mem://pkg", "abc", "def", "tok", {})

    async def fake_resolve(
        session: AsyncSession,
        tenant_id: int,
        agent_version_id: int,
        decrypt: object = None,
    ) -> dict[str, str]:
        return {"REGION": "cn"}

    async def fake_supports(executor_id: int, feature: str) -> bool:
        return False

    monkeypatch.setattr(
        "autowonder.conversations.transport.prepare_capability",
        fake_prepare,
    )
    monkeypatch.setattr(
        "autowonder.conversations.transport.resolve_snapshot",
        fake_resolve,
    )
    monkeypatch.setattr(
        "autowonder.conversations.transport.presence_manager.supports_protocol_feature",
        fake_supports,
    )
    with pytest.raises(ProtocolCompatibilityError, match=AGENT_ENVIRONMENT_VARIABLES_V1):
        await send_turn(
            cast(AsyncSession, object()),
            _conversation(),
            8,
            "hi",
            None,
            1,
            None,
            None,
        )


async def test_send_turn_requires_a_bound_executor() -> None:
    """没有绑定执行器时拒绝组帧。"""
    conversation = _conversation()
    conversation.executor_id = None
    with pytest.raises(IllegalArgumentError, match="conversation must have a bound executor"):
        await send_turn(cast(AsyncSession, object()), conversation, 8, "hi", None, 1, None, None)
