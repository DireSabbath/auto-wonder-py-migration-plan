"""MCP JSON-RPC 协议与工具名登记。"""

from fastapi.testclient import TestClient

from autowonder.main import create_app
from autowonder.mcp.catalog import list_tools
from autowonder.mcp.invoke import TOOL_ACCESS
from autowonder.mcp.principal import Principal
from autowonder.mcp.protocol import (
    accepts_event_stream_only,
    handle_rpc,
    initialize_result,
    omit_nulls,
    resolve_token,
    router,
    rpc_error,
    rpc_ok,
)


def test_accepts_event_stream_only() -> None:
    """只有单独的事件流 Accept 才走 SSE。"""
    assert accepts_event_stream_only(None) is False
    assert accepts_event_stream_only("   ") is False
    assert accepts_event_stream_only("text/event-stream") is True
    assert accepts_event_stream_only("text/event-stream;q=1") is True
    assert accepts_event_stream_only("application/json") is False
    assert accepts_event_stream_only("application/json, text/event-stream") is False
    assert accepts_event_stream_only("*/*") is False


def test_resolve_token_prefers_path_and_falls_back_when_blank() -> None:
    """路径令牌优先；空白路径令牌回退查询参数。"""
    assert resolve_token("awmcp_path", "query") == "awmcp_path"
    assert resolve_token("   ", "query") == "query"
    assert resolve_token(None, "query") == "query"
    assert resolve_token("", None) is None


def test_rpc_response_omits_null_fields() -> None:
    """成功体不写 error，失败体在 id 为 null 时不写 id 和 result。"""
    success = rpc_ok(1, initialize_result())
    assert success["jsonrpc"] == "2.0"
    assert "error" not in success
    assert success["id"] == 1
    failure = rpc_error(None, -32000, "未登录或登录已失效")
    assert failure == {
        "jsonrpc": "2.0",
        "error": {"code": -32000, "message": "未登录或登录已失效"},
    }
    assert "id" not in failure
    assert "result" not in failure


def test_initialize_shape() -> None:
    """initialize 的协议版本、服务名和空 tools 能力与 Java 一致。"""
    body = initialize_result()
    assert body == {
        "protocolVersion": "2025-06-18",
        "serverInfo": {"name": "autowonder", "version": "1.0.0"},
        "capabilities": {"tools": {}},
    }


async def test_unknown_method_is_not_found() -> None:
    """未知 method 返回 JSON-RPC -32601。"""
    principal = Principal.personal(1, 1)
    body = await handle_rpc(None, principal, 4, {"method": "nope"})  # type: ignore[arg-type]
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 4
    assert "result" not in body
    assert body["error"] == {"code": -32601, "message": "Method not found"}


def test_tool_text_omits_null_fields() -> None:
    """工具结果的 text 与 Fastjson 一样不写出 null，数组中的 null 仍保留。"""
    dumped = omit_nulls(
        {
            "items": [
                {
                    "id": 1,
                    "name": "space",
                    "background": None,
                    "version": None,
                    "isOwner": True,
                }
            ],
            "note": None,
        }
    )
    assert dumped == {"items": [{"id": 1, "name": "space", "isOwner": True}]}


def test_event_stream_content_type_has_no_charset() -> None:
    """只接受事件流时，Content-Type 与 Spring 的 text/event-stream 一致，不附加 charset。"""
    client = TestClient(create_app())
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={"Accept": "text/event-stream"},
    )
    assert response.headers["content-type"] == "text/event-stream"
    assert response.text.startswith("data:")
    assert response.text.endswith("\n\n")


def test_rpc_without_token_returns_jsonrpc_error() -> None:
    """白名单上的 POST /api/mcp 无令牌时是 JSON-RPC 错误，不是 HTTP 401。"""
    app = create_app()
    app.include_router(router)
    client = TestClient(app)
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    assert response.status_code != 401
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["error"]["code"] == -32000


def test_catalog_tools_are_registered() -> None:
    """目录里的工具名都有访问级别，避免半登记。"""
    missing = [tool["name"] for tool in list_tools() if tool["name"] not in TOOL_ACCESS]
    assert missing == []
