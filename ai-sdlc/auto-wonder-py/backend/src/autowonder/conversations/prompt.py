"""按渠道拼出下发给执行器的身份提示。"""

from dataclasses import dataclass

from autowonder.agents.evolution import identity_column_text
from autowonder.conversations.constants import (
    API_MODE_SUFFIX,
    CLARIFICATION_CHANNEL,
    CLARIFICATION_PROMPT_SUFFIX,
    INTERNAL_CHANNEL,
    PLATFORM_CHANNEL,
    PLATFORM_PROMPT_SUFFIX,
)
from autowonder.core.errors import BizError, ErrorCode


@dataclass(frozen=True)
class ConversationMode:
    """渠道决定提示后缀，以及是否禁止交互工具。"""

    suffix: str
    one_way: bool


def conversation_mode(channel: str | None) -> ConversationMode:
    """没有渠道或未知渠道按通用模式，不追加后缀。"""
    if channel == PLATFORM_CHANNEL:
        return ConversationMode(PLATFORM_PROMPT_SUFFIX, False)
    if channel == CLARIFICATION_CHANNEL:
        return ConversationMode(CLARIFICATION_PROMPT_SUFFIX, False)
    if channel == INTERNAL_CHANNEL:
        return ConversationMode("", True)
    return ConversationMode("", False)


def identity_body(
    role_name: str | None,
    role_code: str | None,
    business_background: str | None,
    responsibilities: str | None,
    identity_json: object,
) -> str:
    """拼身份段。空字符串字段不写入；整段为空时调用方拒绝下发。"""
    parts: list[str] = []
    _append(parts, "角色", role_name)
    _append(parts, "角色代号", role_code)
    _append(parts, "业务背景", business_background)
    _append(parts, "职责", responsibilities)
    _append(parts, "身份", identity_column_text(identity_json))
    return "".join(parts)


def render_system_prompt(
    role_name: str | None,
    role_code: str | None,
    business_background: str | None,
    responsibilities: str | None,
    identity_json: object,
    channel: str | None,
    acp_supported: bool,
) -> str:
    """在线版本没有身份段时拒绝下发。单向渠道或执行器不能收卡片时禁止交互工具。"""
    body = identity_body(
        role_name,
        role_code,
        business_background,
        responsibilities,
        identity_json,
    )
    if body == "":
        raise BizError(ErrorCode.SYSTEM_ERROR)
    mode = conversation_mode(channel)
    if mode.one_way or not acp_supported:
        body += API_MODE_SUFFIX
    body += mode.suffix
    return body


def _append(parts: list[str], label: str, value: str | None) -> None:
    if value is not None and value != "":
        parts.append(f"{label}: {value}\n")
