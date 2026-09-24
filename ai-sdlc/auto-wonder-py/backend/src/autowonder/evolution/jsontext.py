"""Fastjson 风格的紧凑 JSON，以及 Java 字符串空白。"""

import json
import math

from autowonder.core.errors import BizError, ErrorCode
from autowonder.debuglogs.sanitizer import java_is_blank


def java_trim(value: str) -> str:
    """``String.trim``：只去掉码点不大于 U+0020 的字符。"""
    start = 0
    end = len(value)
    while start < end and ord(value[start]) <= 0x20:
        start += 1
    while end > start and ord(value[end - 1]) <= 0x20:
        end -= 1
    return value[start:end]


def blank(value: str | None) -> bool:
    """``null`` 与 ``String.isBlank`` 都视为空白。"""
    if value is None:
        return True
    return java_is_blank(value)


def dump_json(value: object) -> str:
    """紧凑 JSON。对象里的 null 不写出，与 Fastjson 缺省一致。"""
    return json.dumps(_omit_nulls(value), ensure_ascii=False, separators=(",", ":"))


def parse_json(text: str) -> object:
    """解析 JSON。非法文本是参数不合法。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error


def parse_object(text: str) -> dict[str, object]:
    """解析非空 JSON 对象。"""
    parsed = parse_json(text)
    if not isinstance(parsed, dict) or len(parsed) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    return parsed


def as_dict(value: object) -> dict[str, object] | None:
    """策略 JSON 损坏时没有 action，调用方按缺失继续。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or java_is_blank(value):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def text_field(obj: dict[str, object], key: str) -> str | None:
    """对象字段里的字符串。其他类型不是字符串字段。"""
    value = obj.get(key)
    if isinstance(value, str):
        return value
    return None


def long_field(obj: dict[str, object], key: str) -> int | None:
    """JSON 数字里的整数。布尔值不是整数资产编号。"""
    value = obj.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == math.floor(value):
        return int(value)
    return None


def double_field(obj: dict[str, object], key: str) -> float | None:
    """JSON 数字。布尔值不是置信度。"""
    value = obj.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def bool_field(obj: dict[str, object], key: str) -> bool | None:
    """显式布尔。缺字段与 JSON null 都是没有显式值。"""
    value = obj.get(key)
    if isinstance(value, bool):
        return value
    return None


def first_text(*values: str | None) -> str | None:
    """按书写顺序取第一个非空白字符串。"""
    for value in values:
        if isinstance(value, str) and not java_is_blank(value):
            return value
    return None


def _omit_nulls(value: object) -> object:
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            if item is None:
                continue
            cleaned[key] = _omit_nulls(item)
        return cleaned
    if isinstance(value, list):
        return [_omit_nulls(item) for item in value]
    return value
