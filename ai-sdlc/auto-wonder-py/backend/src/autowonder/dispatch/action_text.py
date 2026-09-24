"""实时活动摘要的乱码识别和脱敏。规则对齐 Java 的默认正则。"""

import re
import unicodedata

DEFAULT_MAX_CHARS = 180
REDACTED = "[REDACTED]"
_ELLIPSIS = "…"
_ASCII = re.ASCII
_ASCII_IGNORE = re.ASCII | re.IGNORECASE

_SIGNED_URL_SECRET = re.compile(
    r"([?&](?:signature|x-amz-signature|x-amz-credential|x-amz-security-token|ossaccesskeyid"
    r"|accesskeyid|accesskeysecret|sig|token|access_token|authtoken|api[-_]?key|secpubk)="
    r")[^&#\s]+",
    _ASCII_IGNORE,
)
_CREDENTIAL_HEADER = re.compile(
    r"\b(authorization\s*[:=]\s*|bearer\s+|basic\s+)[a-z0-9._~+/-]{8,}=*",
    _ASCII_IGNORE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b([a-z0-9_.-]*(?:token|secret|password|passwd|pwd|apikey|api_key|api-key|accesskey"
    r"|access_key|credential|signature|privatekey|private_key|sessionkey|authcode|mcpsecret"
    r"|mcp_secret|clientsecret|client_secret)[a-z0-9_.-]*)\s*[:=]\s*"
    r"([^\s,;\"'}\]]{4,})",
    _ASCII_IGNORE,
)
_JWT = re.compile(r"\beyJ[a-z0-9_-]{6,}\.[a-z0-9_-]{6,}\.[a-z0-9_-]{4,}\b", _ASCII)
_LONG_OPAQUE = re.compile(r"\b[a-z0-9+/_.=-]{32,}\b", _ASCII_IGNORE)
_HEX_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", _ASCII_IGNORE)
_PATH_SAFE = re.compile(r"^[A-Za-z0-9._~/-]+$")
_CONTROL_CHARS = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
_ASCII_SPACE_RUN = re.compile(r"[ \t\n\x0b\x0c\r]{2,}")


def looks_like_mojibake(text: str | None) -> bool:
    """错码文本含替换符，或拉丁扩展字符占可见字符的一半以上。"""
    if text is None or java_is_blank(text):
        return False
    if "\ufffd" in text:
        return True
    suspicious = 0
    visible = 0
    for char in text:
        if not _is_java_whitespace(ord(char)):
            visible += 1
        code = ord(char)
        if 0x00C0 <= code <= 0x024F or code == 0x00A0:
            suspicious += 1
    floor = visible
    if floor < 1:
        floor = 1
    return suspicious >= 3 and suspicious * 2 >= floor


def looks_sensitive(text: str | None) -> bool:
    """清洗后仍能认出签名、口令、JWT 或密钥赋值。"""
    if text is None or java_is_blank(text):
        return False
    if _SIGNED_URL_SECRET.search(text) is not None:
        return True
    if _CREDENTIAL_HEADER.search(text) is not None:
        return True
    if _SECRET_ASSIGNMENT.search(text) is not None:
        return True
    return _JWT.search(text) is not None


def sanitize(text: str | None, max_chars: int = DEFAULT_MAX_CHARS) -> str | None:
    """去掉控制符和密钥形状，再按码点截断。空结果返回空。"""
    if text is None:
        return None
    limit = max_chars
    if limit <= 0:
        limit = DEFAULT_MAX_CHARS
    value = _pre_truncate(text, limit)
    value = _CONTROL_CHARS.sub(" ", value)
    value = _SIGNED_URL_SECRET.sub(r"\1" + REDACTED, value)
    value = _JWT.sub(REDACTED, value)
    value = _CREDENTIAL_HEADER.sub(REDACTED, value)
    value = _SECRET_ASSIGNMENT.sub(r"\1=" + REDACTED, value)
    value = _redact_long_opaque(value)
    value = _ASCII_SPACE_RUN.sub(" ", value)
    value = _java_trim(value)
    if value == "":
        return None
    return truncate(value, limit)


def truncate(text: str | None, max_chars: int) -> str | None:
    """超过码点上限时去掉尾部空白并加上省略号。"""
    if text is None:
        return None
    limit = max_chars
    if limit <= 0:
        limit = DEFAULT_MAX_CHARS
    if _code_points(text) <= limit:
        return text
    cut = _take_code_points(text, limit)
    return _java_strip_trailing(cut) + _ELLIPSIS


def java_is_blank(text: str) -> bool:
    """Java ``String.isBlank``：只把 ``Character.isWhitespace`` 当作空白。"""
    for char in text:
        if not _is_java_whitespace(ord(char)):
            return False
    return True


def _redact_long_opaque(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group()
        if _is_known_safe_token(token):
            return token
        return REDACTED

    return _LONG_OPAQUE.sub(replace, value)


def _is_known_safe_token(token: str | None) -> bool:
    if token is None or token == "":
        return False
    if _HEX_SHA.fullmatch(token) is not None:
        return True
    return "/" in token and _PATH_SAFE.fullmatch(token) is not None


def _pre_truncate(text: str, limit: int) -> str:
    cap = limit * 8
    if _code_points(text) <= cap:
        return text
    return _take_code_points(text, cap)


def _code_points(text: str) -> int:
    return len(text)


def _take_code_points(text: str, count: int) -> str:
    return text[:count]


def _java_trim(text: str) -> str:
    start = 0
    end = len(text)
    while start < end and ord(text[start]) <= 0x20:
        start += 1
    while end > start and ord(text[end - 1]) <= 0x20:
        end -= 1
    return text[start:end]


def _java_strip_trailing(text: str) -> str:
    end = len(text)
    while end > 0 and _is_java_whitespace(ord(text[end - 1])):
        end -= 1
    return text[:end]


def _is_java_whitespace(code_point: int) -> bool:
    if code_point in {0x00A0, 0x2007, 0x202F}:
        return False
    if code_point in {0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F}:
        return True
    category = unicodedata.category(chr(code_point))
    if category == "Zs" or category == "Zl" or category == "Zp":
        return True
    return False
