"""交给执行器的轮次正文。钉钉渠道补上发送人上下文。"""

import json
import logging

logger = logging.getLogger(__name__)

_INSTRUCTION = (
    "\nInstruction: The sender above is the human who sent the current DingTalk message. "
    'If the user asks "who am I" or refers to "me", answer using this DingTalk sender context. '
    "Do not confuse this sender with any AutoWonder MCP token or tool identity.\n"
)


def runtime_content(channel: str, content: str | None, source_context: str | None) -> str | None:
    """非钉钉原样下发。钉钉在用户正文前附上发送人，没有可用字段时仍用原文。"""
    if channel.upper() != "DINGTALK":
        return content
    prefix = dingtalk_sender_context_prompt(source_context)
    if prefix.strip() == "":
        return content
    body = "" if content is None else content
    return prefix + "\nUser message:\n" + body


def dingtalk_sender_context_prompt(source_context: str | None) -> str:
    """从入站 JSON 抽出发送人。空文本或坏 JSON 不附上下文。"""
    if source_context is None or source_context.strip() == "":
        return ""
    try:
        parsed = json.loads(source_context)
    except json.JSONDecodeError as error:
        logger.warning(
            "ignore invalid DingTalk sourceContext for conversation prompt: %s",
            error,
        )
        return ""
    if not isinstance(parsed, dict):
        logger.warning(
            "ignore invalid DingTalk sourceContext for conversation prompt: %s",
            "source context must be an object",
        )
        return ""
    lines = ["DingTalk message context:\n"]
    _append(lines, "- Sender nickname", _text(parsed, "senderNick"))
    _append(lines, "- Sender staffId", _text(parsed, "senderStaffId"))
    _append(lines, "- Sender dingtalk senderId", _text(parsed, "senderId"))
    _append(lines, "- Conversation title", _text(parsed, "conversationTitle"))
    _append(lines, "- Conversation type", _text(parsed, "conversationType"))
    text = "".join(lines)
    if text == "DingTalk message context:\n":
        return ""
    return text + _INSTRUCTION


def _append(lines: list[str], label: str, value: str | None) -> None:
    if value is None or value == "":
        return
    lines.append(label + ": " + value + "\n")


def _text(parsed: dict[str, object], key: str) -> str | None:
    value = parsed.get(key)
    if isinstance(value, str):
        return value
    return None
