"""MCP JSON-RPC 与工具 HTTP 入口，对应 Java ``McpController``。"""

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.context import current
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data, ok
from autowonder.db.session import get_session
from autowonder.mcp.invoke import invoke_tool, list_tools_for_principal
from autowonder.mcp.principal import Principal
from autowonder.mcp.tokens import authenticate

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

_JSON = ("application", "json")
_EVENT_STREAM = ("text", "event-stream")
_PROTOCOL_VERSION = "2025-06-18"


def resolve_token(path_token: str | None, query_token: str | None) -> str | None:
    """路径令牌优先。空白路径令牌回退到查询参数，与 ``resolveToken`` 一致。"""
    if path_token is not None and path_token.strip() != "":
        return path_token
    return query_token


def accepts_event_stream_only(accept: str | None) -> bool:
    """只接受 ``text/event-stream``、且不接受 ``application/json`` 时走 SSE。"""
    if accept is None or accept.strip() == "":
        return False
    accepted = _media_types(accept)
    accepts_json = any(_compatible(item, _JSON) for item in accepted)
    accepts_events = any(_compatible(item, _EVENT_STREAM) for item in accepted)
    return accepts_events and not accepts_json


def initialize_result() -> dict[str, Any]:
    """``initialize`` 的 result。协议版本和服务器信息与 Java 固定值一致。"""
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "serverInfo": {"name": "autowonder", "version": "1.0.0"},
        "capabilities": {"tools": {}},
    }


def rpc_ok(request_id: object, result: object) -> dict[str, Any]:
    """成功响应。null 的 id 和 result 不写出，对齐 ``@JsonInclude(NON_NULL)``。"""
    body: dict[str, Any] = {"jsonrpc": "2.0"}
    if request_id is not None:
        body["id"] = request_id
    if result is not None:
        body["result"] = result
    return body


def rpc_error(request_id: object, code: int, message: str | None) -> dict[str, Any]:
    """失败响应。null 的 id 省略，error 只含 code 与 message。"""
    error: dict[str, Any] = {"code": code}
    if message is not None:
        error["message"] = message
    body: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if request_id is not None:
        body["id"] = request_id
    return body


async def handle_rpc(
    session: AsyncSession,
    principal: Principal,
    request_id: object,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """按 method 分发。未知方法返回 -32601。"""
    method = "null" if request.get("method") is None else str(request.get("method"))
    if method == "initialize":
        return rpc_ok(request_id, initialize_result())
    if method == "tools/list":
        tools = await list_tools_for_principal(session, principal)
        return rpc_ok(request_id, {"tools": dump_data(tools)})
    if method == "tools/call":
        params = as_object_map(request.get("params"))
        name = "null" if params.get("name") is None else str(params.get("name"))
        arguments = as_object_map(params.get("arguments"))
        result = await invoke_tool(session, principal, name, arguments)
        structured = {"items": result} if isinstance(result, list) else result
        dumped = dump_data(structured)
        text = json.dumps(omit_nulls(dumped), ensure_ascii=False, separators=(",", ":"))
        return rpc_ok(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": dumped,
                "isError": False,
            },
        )
    return rpc_error(request_id, -32601, "Method not found")


def omit_nulls(value: object) -> object:
    """Fastjson 默认不写出 null。数组里的 null 保留。"""
    if isinstance(value, dict):
        kept: dict[str, object] = {}
        for key, item in value.items():
            if item is None:
                continue
            kept[key] = omit_nulls(item)
        return kept
    if isinstance(value, list):
        return [omit_nulls(item) for item in value]
    return value


def as_object_map(value: object) -> dict[str, Any]:
    """把 params 或 arguments 收成对象。null 当成空对象。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    parsed = json.loads(encoded)
    if isinstance(parsed, dict):
        return parsed
    raise BizError(ErrorCode.MCP_TOOL_ARGUMENT_INVALID)


@asynccontextmanager
async def principal_context(principal: Principal) -> AsyncIterator[None]:
    """写入用户、工作空间和访问级别，结束后恢复，避免串到后续请求。"""
    ctx = current()
    previous = (ctx.user_id, ctx.trace_id, ctx.workspace_id, ctx.access_level)
    ctx.user_id = principal.user_id
    ctx.trace_id = str(uuid.uuid4())
    if principal.is_workspace_scoped():
        ctx.workspace_id = principal.workspace_id
        ctx.access_level = principal.access_level.name  # type: ignore[union-attr]
    try:
        yield
    finally:
        ctx.user_id, ctx.trace_id, ctx.workspace_id, ctx.access_level = previous


@router.get("/tools")
async def list_tools(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """列出当前凭证可见的工具。"""
    principal = await authenticate(session, authorization, token)
    async with principal_context(principal):
        tools = await list_tools_for_principal(session, principal)
        return ok(tools)


@router.post("/tools/call")
async def call_tool(
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按名称调用一个工具。"""
    principal = await authenticate(session, authorization, token)
    async with principal_context(principal):
        result = await invoke_tool(
            session,
            principal,
            "null" if body.get("name") is None else str(body.get("name")),
            as_object_map(body.get("arguments")),
        )
        return ok(result)


@router.post("")
@router.post("/rpc")
@router.post("/rpc/")
async def rpc(
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    accept: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """固定路径上的 JSON-RPC。"""
    return await _rpc(session, authorization, token, accept, body)


@router.post("/{path_token}")
@router.post("/{path_token}/")
@router.post("/{path_token}/rpc")
@router.post("/{path_token}/rpc/")
async def rpc_with_path_token(
    path_token: str,
    body: dict[str, Any],
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    accept: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """带路径令牌的 JSON-RPC。注册在固定路径之后，避免吞掉 tools 和 rpc。"""
    return await _rpc(session, authorization, resolve_token(path_token, token), accept, body)


async def _rpc(
    session: AsyncSession,
    authorization: str | None,
    token: str | None,
    accept: str | None,
    body: dict[str, Any],
) -> Response:
    if "id" not in body:
        principal = await authenticate(session, authorization, token)
        async with principal_context(principal):
            return Response(status_code=202)
    try:
        principal = await authenticate(session, authorization, token)
        async with principal_context(principal):
            response = await handle_rpc(session, principal, body.get("id"), body)
    except BizError as error:
        response = rpc_error(body.get("id"), -32000, str(error))
    except Exception as error:
        response = rpc_error(body.get("id"), -32603, str(error))
    if accepts_event_stream_only(accept):
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        return Response(
            content="data:" + payload + "\n\n",
            headers={"content-type": "text/event-stream"},
        )
    return JSONResponse(content=response)


def _media_types(accept: str) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    for part in accept.split(","):
        raw = part.split(";", 1)[0].strip().lower()
        if "/" not in raw:
            continue
        media_type, subtype = raw.split("/", 1)
        parsed.append((media_type, subtype))
    return parsed


def _compatible(left: tuple[str, str], right: tuple[str, str]) -> bool:
    left_type, left_subtype = left
    right_type, right_subtype = right
    type_matches = left_type == "*" or right_type == "*" or left_type == right_type
    if not type_matches:
        return False
    if left_subtype == "*" or right_subtype == "*" or left_subtype == right_subtype:
        return True
    return _suffix(left_subtype) == right_subtype or _suffix(right_subtype) == left_subtype


def _suffix(subtype: str) -> str | None:
    plus = subtype.rfind("+")
    if plus < 0:
        return None
    return subtype[plus + 1 :]
