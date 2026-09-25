"""外部操作回执里的密钥脱敏。"""

import json
import re

_INLINE_SECRET = re.compile(
    r"(?i)(authorization|access[-_ ]?key|token|secret|password)(\s*[:=]\s*)([^\s,;]+)"
)
_MAX_ERROR_LENGTH = 2000


def sanitize_text(value: str | None) -> str | None:
    """把行内 ``token=...`` 一类片段换成 ``[REDACTED]``。"""
    if value is None:
        return None
    return _INLINE_SECRET.sub(r"\1\2[REDACTED]", value)


def sanitize_error(value: str | None) -> str:
    """错误摘要先脱敏，再截到 2000 字。"""
    raw = "external operation failed" if value is None or value.strip() == "" else value
    sanitized = sanitize_text(raw) or ""
    if len(sanitized) <= _MAX_ERROR_LENGTH:
        return sanitized
    return sanitized[:_MAX_ERROR_LENGTH]


def sanitize_json(payload: str | None) -> str:
    """按字段名抹掉凭据，再序列化。"""
    parsed = json.loads("{}" if payload is None or payload.strip() == "" else payload)
    return json.dumps(_sanitize_value(None, parsed), ensure_ascii=False, separators=(",", ":"))


def _sanitize_value(key: str | None, value: object) -> object:
    if _secret_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {child: _sanitize_value(child, item) for child, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(None, item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _secret_key(key: str | None) -> bool:
    if key is None:
        return False
    normalized = key.lower().replace("_", "").replace("-", "")
    return (
        "authorization" in normalized
        or "accesstoken" in normalized
        or "accesskey" in normalized
        or "secret" in normalized
        or "password" in normalized
        or normalized == "token"
        or "credential" in normalized
    )
