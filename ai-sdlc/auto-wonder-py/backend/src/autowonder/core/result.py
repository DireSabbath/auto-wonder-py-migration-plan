"""Result 信封。字段名与 Java Jackson/Fastjson 契约一致。"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from autowonder.core.context import current_request_id, current_trace_id
from autowonder.core.errors import ErrorCode


def dump_data(data: Any) -> Any:
    """把 Pydantic 模型按 camelCase 别名收成 JSON 对象。"""
    if isinstance(data, BaseModel):
        return data.model_dump(by_alias=True, mode="json")
    if isinstance(data, list):
        return [dump_data(item) for item in data]
    if isinstance(data, Mapping):
        return {key: dump_data(value) for key, value in data.items()}
    return data


def ok(data: Any) -> dict[str, Any]:
    """成功信封。``message`` 为空字符串，与 ``Result.ok`` 一致。"""
    return {
        "success": True,
        "code": ErrorCode.SUCCESS.code,
        "message": "",
        "data": dump_data(data),
        "traceId": current_trace_id(),
        "request_id": current_request_id(),
    }


def fail(
    error_code: ErrorCode,
    message: str | None = None,
    data: Any = None,
) -> dict[str, Any]:
    """失败信封，保留空字段，对齐 Spring Jackson 序列化。"""
    return {
        "success": False,
        "code": error_code.code,
        "message": error_code.message if message is None else message,
        "data": dump_data(data),
        "traceId": current_trace_id(),
        "request_id": current_request_id(),
    }


def fail_omitting_nulls(
    error_code: ErrorCode,
    message: str | None = None,
) -> dict[str, Any]:
    """鉴权过滤器失败体。Fastjson 默认不写出 null，这里同样省略。"""
    body: dict[str, Any] = {
        "success": False,
        "code": error_code.code,
        "message": error_code.message if message is None else message,
    }
    trace_id = current_trace_id()
    request_id = current_request_id()
    if trace_id is not None:
        body["traceId"] = trace_id
    if request_id is not None:
        body["request_id"] = request_id
    return body
