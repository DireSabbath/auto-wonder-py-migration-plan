"""成员身份标签。条数、长度和 JSON 形状与 Java ``IdentityTags`` 一致。"""

import json

from autowonder.core.errors import BizError, ErrorCode

MAX_TAGS = 8
MAX_TAG_LENGTH = 32


def java_length(text: str) -> int:
    """``String.length()`` 统计的是 UTF-16 代码单元。"""
    return len(text.encode("utf-16-le")) // 2


def normalize(tags: list[str] | None) -> list[str]:
    """去空白、去重，并执行 8 条 / 32 字符上限。"""
    if tags is None:
        return []
    normalized: dict[str, None] = {}
    for tag in tags:
        trimmed = tag.strip()
        if trimmed == "":
            continue
        if java_length(trimmed) > MAX_TAG_LENGTH:
            raise BizError(
                ErrorCode.PARAM_INVALID,
                "Identity tags must not exceed 32 Java characters per tag",
            )
        normalized[trimmed] = None
        if len(normalized) > MAX_TAGS:
            raise BizError(
                ErrorCode.PARAM_INVALID,
                "Identity tags must not contain more than 8 tags",
            )
    return list(normalized)


def to_json(tags: list[str] | None) -> str:
    """紧凑 JSON 数组，供仍按字符串读写的路径使用。"""
    return json.dumps(normalize(tags), ensure_ascii=False, separators=(",", ":"))


def from_stored(value: object) -> list[str]:
    """读取 JSON 列或历史字符串。空值是空列表，坏形状是参数错误。"""
    if value is None:
        return []
    if isinstance(value, str):
        return _from_json_text(value)
    return _from_parsed(value)


def _from_json_text(text: str) -> list[str]:
    if text.strip() == "":
        raise _malformed()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise _malformed() from error
    return _from_parsed(parsed)


def _from_parsed(parsed: object) -> list[str]:
    if parsed is None:
        return []
    if not isinstance(parsed, list):
        raise _malformed()
    tags: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            raise _malformed()
        tags.append(item)
    return normalize(tags)


def _malformed() -> BizError:
    return BizError(ErrorCode.PARAM_INVALID, "Invalid persisted identity tags JSON")
