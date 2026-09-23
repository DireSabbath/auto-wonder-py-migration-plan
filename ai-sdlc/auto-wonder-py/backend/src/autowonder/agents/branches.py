"""仓库提交分支规则。只允许一个结尾通配符，其余必须是合法 Git 分支。"""

import json
import unicodedata

from autowonder.core.errors import BizError, ErrorCode

_MAX_PATTERNS = 32
_MAX_PATTERN_BYTES = 255
_WILDCARD_SENTINEL = "autowonder-pattern-check"
_FORBIDDEN = set("~^:?*[\\")
_STORAGE_MESSAGE = "仓库提交分支规则存储格式不合法"


def normalize(patterns: list[str | None] | None) -> list[str]:
    """去重并保持原顺序。空列表表示不限制。"""
    if patterns is None or len(patterns) == 0:
        return []
    if len(patterns) > _MAX_PATTERNS:
        raise BizError(ErrorCode.PARAM_INVALID, f"提交分支规则最多允许 {_MAX_PATTERNS} 条")
    unique: dict[str, None] = {}
    for pattern in patterns:
        if pattern is None or pattern == "":
            raise BizError(ErrorCode.PARAM_INVALID, "提交分支规则不能为空")
        if _edge_whitespace_or_control(pattern):
            raise BizError(
                ErrorCode.PARAM_INVALID,
                "提交分支规则不能包含首尾空白或控制字符: " + pattern,
            )
        if len(pattern.encode("utf-8")) > _MAX_PATTERN_BYTES:
            raise BizError(
                ErrorCode.PARAM_INVALID,
                f"提交分支规则不能超过 {_MAX_PATTERN_BYTES} 个 UTF-8 字节",
            )
        if not _single_trailing_wildcard(pattern):
            raise BizError(ErrorCode.PARAM_INVALID, "提交分支规则只允许一个结尾通配符: " + pattern)
        if not _is_valid_branch(_candidate(pattern)):
            raise BizError(ErrorCode.PARAM_INVALID, "提交分支规则不是合法的 Git 分支: " + pattern)
        unique[pattern] = None
    return list(unique)


def encode(patterns: list[str | None] | None) -> str | None:
    """空规则存 null。非空存紧凑 JSON 数组。"""
    normalized = normalize(patterns)
    if len(normalized) == 0:
        return None
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def decode(raw: str | None) -> list[str]:
    """读回已存储的规则。格式损坏时按参数错误拒绝。"""
    if raw is None or raw.strip() == "":
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID, _STORAGE_MESSAGE) from error
    if not isinstance(parsed, list):
        raise BizError(ErrorCode.PARAM_INVALID, _STORAGE_MESSAGE)
    patterns: list[str | None] = []
    for item in parsed:
        if not isinstance(item, str):
            raise BizError(ErrorCode.PARAM_INVALID, _STORAGE_MESSAGE)
        patterns.append(item)
    return normalize(patterns)


def _single_trailing_wildcard(pattern: str) -> bool:
    first = pattern.find("*")
    if first < 0:
        return True
    if first != len(pattern) - 1:
        return False
    return pattern.find("*", first + 1) < 0


def _candidate(pattern: str) -> str:
    if pattern.endswith("*") and pattern.find("*") == len(pattern) - 1:
        return pattern[:-1] + _WILDCARD_SENTINEL
    return pattern


def _is_valid_branch(branch: str) -> bool:
    if branch == "" or branch == "@" or branch.startswith("-"):
        return False
    if branch.startswith("/") or branch.endswith("/") or branch.endswith("."):
        return False
    if ".." in branch or "@{" in branch or "//" in branch:
        return False
    for unit in _utf16_units(branch):
        if unit <= 0x20 or unit == 0x7F or chr(unit) in _FORBIDDEN:
            return False
    for component in branch.split("/"):
        if component == "" or component.startswith(".") or component.endswith(".lock"):
            return False
    return True


def _edge_whitespace_or_control(pattern: str) -> bool:
    first = ord(pattern[0])
    last = ord(pattern[-1])
    if _is_whitespace(first) or _is_whitespace(last):
        return True
    for char in pattern:
        if _is_iso_control(ord(char)):
            return True
    return False


def _is_whitespace(code_point: int) -> bool:
    char = chr(code_point)
    if char.isspace():
        return True
    return unicodedata.category(char) in {"Zs", "Zl", "Zp"}


def _is_iso_control(code_point: int) -> bool:
    if code_point <= 0x1F:
        return True
    return 0x7F <= code_point <= 0x9F


def _utf16_units(text: str) -> list[int]:
    encoded = text.encode("utf-16-le")
    return [
        int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)
    ]
