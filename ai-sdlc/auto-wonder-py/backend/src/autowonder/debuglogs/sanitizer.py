"""debug_log 入库前的列宽整理。

sha256、error_message 按 MySQL 的 code point 宽度收敛。控制字符替换只用于
sha256 兜底和日志里的 channel，error_message 只截断、不改字符。
"""

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

MAX_ERROR_MESSAGE_CHARS = 1024
MAX_SHA256_CHARS = 80
MAX_LOGGED_CHANNEL_CHARS = 32
SHA256_PREFIX = "sha256:"
SHA256_HEX = re.compile("^[0-9a-fA-F]{64}$")
ALLOWED_UPLOAD_CHANNELS = frozenset({"DIRECT", "RELAY"})

_NON_BREAKING_SPACE = frozenset({0x00A0, 0x2007, 0x202F})
_CONTROL_WHITESPACE = frozenset(
    {0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F}
)


def java_is_whitespace(code_point: int) -> bool:
    """与 ``Character.isWhitespace`` 一致，不把不换行空格当成空白。"""
    if code_point in _NON_BREAKING_SPACE:
        return False
    if code_point in _CONTROL_WHITESPACE:
        return True
    category = unicodedata.category(chr(code_point))
    if category == "Zs" or category == "Zl" or category == "Zp":
        return True
    return False


def java_is_blank(text: str) -> bool:
    """与 ``String.isBlank`` 一致。"""
    for char in text:
        if not java_is_whitespace(ord(char)):
            return False
    return True


def java_strip(text: str) -> str:
    """与 ``String.strip`` 一致，按 code point 去掉两端 Java 空白。"""
    chars = list(text)
    start = 0
    end = len(chars)
    while start < end and java_is_whitespace(ord(chars[start])):
        start += 1
    while end > start and java_is_whitespace(ord(chars[end - 1])):
        end -= 1
    return "".join(chars[start:end])


def is_iso_control(code_point: int) -> bool:
    """与 ``Character.isISOControl`` 一致。"""
    if code_point <= 0x1F:
        return True
    return 0x7F <= code_point <= 0x9F


def truncate_code_points(value: str, max_code_points: int) -> str:
    """按 code point 截断，不把代理对切成一半。"""
    parts: list[str] = []
    for char in value:
        if len(parts) == max_code_points:
            break
        parts.append(char)
    return "".join(parts)


def truncate_and_strip_control(value: str, max_code_points: int) -> str:
    """按 code point 截断，并把 ISO 控制字符换成 ``-``。"""
    parts: list[str] = []
    kept = 0
    for char in value:
        if kept == max_code_points:
            break
        kept += 1
        if is_iso_control(ord(char)):
            parts.append("-")
        else:
            parts.append(char)
    return "".join(parts)


def truncate_error_message(error: str | None) -> str | None:
    """error_message 超过 1024 个 code point 时截断。"""
    if error is None:
        return None
    count = 0
    for _char in error:
        count += 1
        if count > MAX_ERROR_MESSAGE_CHARS:
            return truncate_code_points(error, MAX_ERROR_MESSAGE_CHARS)
    return error


def sanitize_sha256(value: str | None) -> str | None:
    """裸 64 位 hex 原样保留；``sha256:`` 前缀在余下仍是裸 hex 时剥掉。"""
    if value is None:
        return None
    if java_is_blank(value):
        return None
    if SHA256_HEX.fullmatch(value):
        return value
    if value.startswith(SHA256_PREFIX):
        bare = value[len(SHA256_PREFIX) :]
        if SHA256_HEX.fullmatch(bare):
            return bare
    return truncate_and_strip_control(value, MAX_SHA256_CHARS)


def logged_channel(channel: str) -> str:
    """脏 channel 写入日志前截到 32 个 code point，并去掉控制字符。"""
    return truncate_and_strip_control(channel, MAX_LOGGED_CHANNEL_CHARS)


def accepted_upload_channel(dispatch_id: int, channel: str | None) -> str | None:
    """只接受 DIRECT 和 RELAY。其他值记警告并当成空。"""
    if channel is None:
        return None
    if channel in ALLOWED_UPLOAD_CHANNELS:
        return channel
    logger.warning(
        "debug log report bad channel dispatchId=%s channel=%s "
        "reason=DEBUG_LOG_REPORT_BAD_CHANNEL",
        dispatch_id,
        logged_channel(channel),
    )
    return None
