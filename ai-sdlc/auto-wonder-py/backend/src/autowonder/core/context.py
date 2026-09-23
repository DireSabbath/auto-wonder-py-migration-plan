"""请求级上下文，对应 Java ``AutoWonderContext`` 的线程局部变量。"""

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass
class RequestContext:
    """一次 HTTP 请求上挂的身份与日志字段。"""

    request_id: str | None = None
    trace_id: str | None = None
    operation: str | None = None
    user_id: int | None = None
    workspace_id: int | None = None
    access_level: str | None = None


_CONTEXT: ContextVar[RequestContext | None] = ContextVar("autowonder_context", default=None)


def current() -> RequestContext:
    """返回当前上下文；尚未建立时创建空上下文。"""
    value = _CONTEXT.get()
    if value is None:
        value = RequestContext()
        _CONTEXT.set(value)
    return value


def set_context(value: RequestContext) -> Token[RequestContext | None]:
    """替换当前上下文，并返回用于复位的 token。"""
    return _CONTEXT.set(value)


def reset_context(token: Token[RequestContext | None]) -> None:
    """请求结束后复位上下文，避免同一任务复用时串数据。"""
    _CONTEXT.reset(token)


def current_workspace_id() -> int | None:
    """供 tenant 条件读取的工作空间 id。"""
    value = _CONTEXT.get()
    if value is None:
        return None
    return value.workspace_id


def current_request_id() -> str | None:
    """信封 ``request_id`` 字段。"""
    value = _CONTEXT.get()
    if value is None:
        return None
    return value.request_id


def current_trace_id() -> str | None:
    """信封 ``traceId`` 字段。"""
    value = _CONTEXT.get()
    if value is None:
        return None
    return value.trace_id


def current_user_id() -> int | None:
    """已通过鉴权的用户 id。"""
    value = _CONTEXT.get()
    if value is None:
        return None
    return value.user_id
