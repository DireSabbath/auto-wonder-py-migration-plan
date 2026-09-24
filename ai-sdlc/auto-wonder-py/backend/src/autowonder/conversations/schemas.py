"""会话 HTTP 的请求与响应。字段经 ``ApiModel`` 使用 camelCase。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class PlatformConversationRequest(ApiModel):
    """新建平台管家会话。标题可省略，随后按首条消息生成。"""

    agent_id: int | None = None
    title: str | None = None


class PlatformConversationPatchRequest(ApiModel):
    """改名与归档。省略的字段表示这次不动它。"""

    title: str | None = None
    archived: bool | None = None


class PlatformShareRequest(ApiModel):
    """把会话只读分享给一名同工作空间成员。"""

    grantee_user_id: int | None = None


class PlatformTurnRequest(ApiModel):
    """发送一条平台管家消息。``clientMessageId`` 用来去重。"""

    content: str | None = None
    client_message_id: str | None = None


class ClarificationConversationRequest(ApiModel):
    """在工单下新建澄清会话。"""

    agent_id: int | None = None


class ClarificationTurnRequest(ApiModel):
    """发送一条工单澄清消息。"""

    content: str | None = None
    client_message_id: str | None = None


class ElicitationReplyRequest(ApiModel):
    """问答卡片的回答。``content`` 是答案 JSON 对象的原文。"""

    action: str | None = None
    content: str | None = None


class TurnView(ApiModel):
    """一轮入站或出站消息。"""

    id: int | None = None
    direction: str | None = None
    content: str | None = None
    status: str | None = None
    error: str | None = None
    gmt_create: datetime | None = None


class ShareView(ApiModel):
    """一条仍有效的只读分享。"""

    grantee_user_id: int | None = None
    permission: str | None = None
    gmt_create: datetime | None = None


class ElicitationView(ApiModel):
    """未解决的问答卡片。``requestedSchema`` 原样回给前端。"""

    request_id: str | None = None
    turn_id: int | None = None
    mode: str | None = None
    message: str | None = None
    requested_schema: str | None = None
    status: str | None = None
    gmt_create: datetime | None = None


class SlashCommandInput(ApiModel):
    """斜杠命令的输入提示。"""

    hint: str | None = None


class SlashCommandView(ApiModel):
    """会话级斜杠命令，只用于补全面板。"""

    name: str | None = None
    description: str | None = None
    input: SlashCommandInput | None = None


class TurnEventView(ApiModel):
    """一轮执行事件的一个分片。"""

    id: int | None = None
    tenant_id: int | None = None
    conversation_id: int | None = None
    turn_id: int | None = None
    dispatch_attempt: int | None = None
    event_seq: int | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
    event_type: str | None = None
    payload_fragment: str | None = None
    gmt_create: datetime | None = None


class PlatformConversationView(ApiModel):
    """平台管家会话。列表不带轮次、分享和卡片。"""

    id: int | None = None
    owner_user_id: int | None = None
    owner: bool = False
    agent_id: int | None = None
    agent_name: str | None = None
    channel_conversation_id: str | None = None
    title: str | None = None
    title_source: str | None = None
    status: str | None = None
    executor_online: bool = False
    streaming_supported: bool = False
    cancel_supported: bool = False
    acp_interaction_supported: bool = False
    attachment_manifest_supported: bool = False
    artifact_output_supported: bool = False
    action_plan_supported: bool = False
    cli_session_ref: str | None = None
    processing_status: str | None = None
    processing_turn_id: int | None = None
    archived_at: datetime | None = None
    last_turn_at: datetime | None = None
    gmt_create: datetime | None = None
    turns: list[TurnView] | None = None
    shares: list[ShareView] | None = None
    pending_elicitations: list[ElicitationView] | None = None
    available_commands: list[SlashCommandView] | None = None


class ClarificationConversationView(ApiModel):
    """工单澄清会话。列表里的卡片和命令是空数组。"""

    id: int | None = None
    agent_id: int | None = None
    agent_name: str | None = None
    channel_conversation_id: str | None = None
    status: str | None = None
    executor_online: bool = False
    streaming_supported: bool = False
    cancel_supported: bool = False
    acp_interaction_supported: bool = False
    cli_session_ref: str | None = None
    processing_status: str | None = None
    processing_turn_id: int | None = None
    last_turn_at: datetime | None = None
    gmt_create: datetime | None = None
    turns: list[TurnView] | None = None
    pending_elicitations: list[ElicitationView] | None = None
    available_commands: list[SlashCommandView] | None = None
