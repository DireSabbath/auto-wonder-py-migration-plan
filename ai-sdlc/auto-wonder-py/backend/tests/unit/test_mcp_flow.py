"""MCP 握手响应的形状，与 Java ``McpController`` 的固定字段一致。"""

from verify.mcp_flow import (
    initialize_matches,
    project_ids,
    rest_project_ids,
    rest_tool_names,
    tool_names,
)

_HANDSHAKE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "protocolVersion": "2025-06-18",
        "serverInfo": {"name": "autowonder", "version": "1.0.0"},
        "capabilities": {"tools": {}},
    },
}


def test_initialize_matches_the_java_handshake() -> None:
    """协议版本、服务名和空 tools 能力都在，且成功体不带 error。"""
    assert initialize_matches(_HANDSHAKE) is True
    failed = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "未登录或登录已失效"},
    }
    assert initialize_matches(failed) is False


def test_tool_payloads_keep_names_and_workspace_ids() -> None:
    """tools/list 与 tools/call 只认 Java 写出的字段。"""
    listed = {"result": {"tools": [{"name": "autowonder.list_projects"}]}}
    called = {
        "result": {
            "structuredContent": {"items": [{"id": 10011, "name": "AW MCP"}]},
            "isError": False,
        }
    }
    rest_tools = {"success": True, "data": [{"name": "autowonder.list_projects"}]}
    rest_call = {"success": True, "data": [{"id": 10011}]}
    assert tool_names(listed) == ["autowonder.list_projects"]
    assert project_ids(called) == [10011]
    assert rest_tool_names(rest_tools) == ["autowonder.list_projects"]
    assert rest_project_ids(rest_call) == [10011]
