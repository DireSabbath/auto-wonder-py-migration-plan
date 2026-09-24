"""签发个人 MCP 令牌，完成 initialize，再调用 list_projects。

明文令牌只留在这次进程里。JSON verdict 不写出它。
"""

import json
import secrets
import time
from typing import Any
from urllib.parse import quote

import httpx

_PROTOCOL_VERSION = "2025-06-18"
_SERVER_INFO = {"name": "autowonder", "version": "1.0.0"}
_LIST_PROJECTS = "autowonder.list_projects"


def mcp_flow(base_url: str) -> dict[str, object]:
    """注册、建工作空间、握手，并用个人令牌列出该空间。"""
    chain = _Chain(base_url.rstrip("/"))
    chain.walk()
    chain.close()
    return chain.verdict()


def initialize_matches(body: object) -> bool:
    """``initialize`` 的协议版本、服务信息和空 tools 能力与 Java 固定值一致。"""
    if not isinstance(body, dict):
        return False
    result = body.get("result")
    if not isinstance(result, dict):
        return False
    if "error" in body:
        return False
    return (
        body.get("jsonrpc") == "2.0"
        and result.get("protocolVersion") == _PROTOCOL_VERSION
        and result.get("serverInfo") == _SERVER_INFO
        and result.get("capabilities") == {"tools": {}}
    )


def project_ids(call_body: object) -> list[int]:
    """从 ``tools/call`` 的 structuredContent.items 取出工作空间编号。"""
    if not isinstance(call_body, dict):
        return []
    result = call_body.get("result")
    if not isinstance(result, dict):
        return []
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        return []
    items = structured.get("items")
    return _ids(items)


def tool_names(list_body: object) -> list[str]:
    """从 ``tools/list`` 的 result.tools 取出工具名。"""
    if not isinstance(list_body, dict):
        return []
    result = list_body.get("result")
    if not isinstance(result, dict):
        return []
    return _names(result.get("tools"))


def rest_project_ids(document: object) -> list[int]:
    """从工具 HTTP 调用的 Result.data 取出工作空间编号。"""
    if not isinstance(document, dict):
        return []
    return _ids(document.get("data"))


def rest_tool_names(document: object) -> list[str]:
    """从工具目录 HTTP 响应的 Result.data 取出工具名。"""
    if not isinstance(document, dict):
        return []
    return _names(document.get("data"))


class _Check:
    def __init__(self, name: str, passed: bool, http: int | None, note: str) -> None:
        self.name = name
        self.passed = passed
        self.http = http
        self.note = note

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "pass": self.passed,
            "http": self.http,
            "note": self.note,
        }


class _Chain:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.client = httpx.Client(timeout=60.0)
        self.checks: list[_Check] = []
        self.facts: dict[str, object] = {}
        self.pass_count = 0
        self.fail_count = 0
        self.stopped = ""
        self._access = ""
        self._mcp = ""
        self._workspace_id = ""

    def close(self) -> None:
        self.client.close()

    def walk(self) -> None:
        username = "aw-mcp-" + str(int(time.time()))
        password = secrets.token_urlsafe(18)
        self.facts["testUser"] = username
        self._register(username, password)
        if self.stopped != "":
            return
        self._login(username, password)
        if self.stopped != "":
            return
        self._workspace(username)
        if self.stopped != "":
            return
        self._issue_token()
        if self.stopped != "":
            return
        self._handshake()
        if self.stopped != "":
            return
        self._list_and_call()

    def verdict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "command": "mcp",
            "ok": self.fail_count == 0 and self.stopped == "",
            "passCount": self.pass_count,
            "failCount": self.fail_count,
            "checks": [item.as_dict() for item in self.checks],
            "facts": self.facts,
        }
        if self.stopped != "":
            body["stopped"] = self.stopped
        return body

    def _register(self, username: str, password: str) -> None:
        self._api(
            "POST",
            "/api/auth/register",
            None,
            {
                "username": username,
                "password": password,
                "email": username + "@example.invalid",
                "nickname": "AW MCP",
            },
        )
        user_id = _json_get(self._document, "data.id")
        self.facts["userId"] = user_id
        self._expect("register", _positive_id(user_id), "register returned an id")

    def _login(self, username: str, password: str) -> None:
        self._api(
            "POST",
            "/api/auth/login",
            None,
            {"username": username, "password": password},
        )
        token = _json_get(self._document, "data.accessToken")
        self.facts["accessTokenPresent"] = token != ""
        if token == "":
            self._expect("login", False, "login returned an access token")
            return
        self._access = token
        self._expect("login", True, "login returned an access token")

    def _workspace(self, username: str) -> None:
        self._api(
            "POST",
            "/api/workspaces",
            self._access,
            {
                "name": "AW MCP " + username,
                "description": "created by verify mcp",
                "background": "mcp handshake",
            },
        )
        workspace_id = _json_get(self._document, "data.id")
        self.facts["workspaceId"] = workspace_id
        self._workspace_id = workspace_id
        self._expect("workspace", _positive_id(workspace_id), "workspace id is present")

    def _issue_token(self) -> None:
        self._api("POST", "/api/mcp/tokens", self._access, {"name": "mcp handshake"})
        token = _json_get(self._document, "data.token")
        prefix = _json_get(self._document, "data.tokenPrefix")
        self.facts["mcpTokenPresent"] = token.startswith("awmcp_")
        self.facts["mcpTokenPrefix"] = prefix
        if not token.startswith("awmcp_"):
            self._expect("issue_token", False, "issued token keeps the awmcp_ prefix")
            return
        self._mcp = token
        self._expect("issue_token", True, "issued token keeps the awmcp_ prefix")

    def _handshake(self) -> None:
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        self._rpc("/api/mcp", initialize, "application/json")
        matched = initialize_matches(self._document)
        self.facts["protocolVersion"] = _json_get(self._document, "result.protocolVersion")
        self.facts["serverName"] = _json_get(self._document, "result.serverInfo.name")
        self._expect("initialize", matched, "initialize matches the Java handshake")
        if self.stopped != "":
            return
        self._rpc(
            "/api/mcp",
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            "application/json, text/event-stream",
        )
        self.facts["initializedStatus"] = self._status
        self._expect(
            "initialized",
            self._status == 202 and self._raw == "",
            "notification is accepted with an empty body",
        )
        if self.stopped != "":
            return
        self._rpc("/api/mcp", initialize, "text/event-stream")
        sse = self._status == 200 and self._raw.startswith("data:") and "2025-06-18" in self._raw
        content_type = self._content_type
        self.facts["sseContentType"] = content_type
        self._expect(
            "initialize_sse",
            sse and "text/event-stream" in content_type,
            "event-stream-only Accept returns one SSE event",
        )
        if self.stopped != "":
            return
        path = "/api/mcp/" + quote(self._mcp, safe="") + "/rpc"
        self._rpc(path, initialize, "application/json", query=False)
        self._expect(
            "initialize_path_token",
            initialize_matches(self._document),
            "path token initializes the same handshake",
        )

    def _list_and_call(self) -> None:
        self._rpc(
            "/api/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            "application/json",
        )
        names = tool_names(self._document)
        self.facts["toolCount"] = len(names)
        self._expect(
            "tools_list",
            _LIST_PROJECTS in names and "error" not in _mapping(self._document),
            "tools/list includes list_projects",
        )
        if self.stopped != "":
            return
        self._rpc(
            "/api/mcp",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": _LIST_PROJECTS, "arguments": {}},
            },
            "application/json",
        )
        ids = project_ids(self._document)
        result = _mapping(_child(self._document, "result"))
        self.facts["listedProjectIds"] = ids
        self.facts["callIsError"] = result.get("isError")
        seen = _positive_id(self._workspace_id) and int(self._workspace_id) in ids
        self._expect(
            "tools_call",
            seen and result.get("isError") is False and "error" not in _mapping(self._document),
            "list_projects returns the new workspace",
        )
        if self.stopped != "":
            return
        self._api("GET", "/api/mcp/tools", None, None, mcp=True)
        rest_names = rest_tool_names(self._document)
        self._expect(
            "rest_tools",
            _json_get(self._document, "success") == "true" and _LIST_PROJECTS in rest_names,
            "GET /api/mcp/tools lists the same tool",
        )
        if self.stopped != "":
            return
        self._api(
            "POST",
            "/api/mcp/tools/call",
            None,
            {"name": _LIST_PROJECTS, "arguments": {}},
            mcp=True,
        )
        rest_ids = rest_project_ids(self._document)
        self.facts["restProjectIds"] = rest_ids
        self._expect(
            "rest_call",
            _json_get(self._document, "success") == "true"
            and _positive_id(self._workspace_id)
            and int(self._workspace_id) in rest_ids,
            "POST /api/mcp/tools/call returns the new workspace",
        )

    def _expect(self, name: str, passed: bool, note: str) -> None:
        http = self._status
        self.checks.append(_Check(name, passed, http, note))
        if passed:
            self.pass_count += 1
            return
        self.fail_count += 1
        if self.stopped == "":
            self.stopped = name

    def _api(
        self,
        method: str,
        path: str,
        token: str | None,
        body: dict[str, object] | None,
        mcp: bool = False,
    ) -> None:
        headers: dict[str, str] = {}
        params: dict[str, str] | None = None
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if mcp:
            params = {"token": self._mcp}
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body).encode()
        self._send(method, path, headers, params, content)

    def _rpc(self, path: str, body: dict[str, object], accept: str, query: bool = True) -> None:
        headers = {"Content-Type": "application/json", "Accept": accept}
        params = {"token": self._mcp} if query else None
        self._send("POST", path, headers, params, json.dumps(body).encode())

    def _send(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        params: dict[str, str] | None,
        content: bytes | None,
    ) -> None:
        try:
            response = self.client.request(
                method,
                self.base_url + path,
                headers=headers,
                params=params,
                content=content,
            )
        except httpx.HTTPError as error:
            self._status = None
            self._raw = ""
            self._document = None
            self._content_type = ""
            self.facts["transportError"] = type(error).__name__
            return
        self._status = response.status_code
        self._raw = response.text
        self._content_type = response.headers.get("content-type", "")
        self._document = _parse(response)

    _status: int | None = None
    _raw: str = ""
    _document: object = None
    _content_type: str = ""


def _parse(response: httpx.Response) -> object:
    if response.content == b"":
        return None
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def _ids(items: object) -> list[int]:
    if not isinstance(items, list):
        return []
    found: list[int] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            found.append(item["id"])
    return found


def _names(tools: object) -> list[str]:
    if not isinstance(tools, list):
        return []
    found: list[str] = []
    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            found.append(tool["name"])
    return found


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _child(document: object, dotted: str) -> object:
    current = document
    for key in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def _json_get(document: object, dotted: str) -> str:
    current = _child(document, dotted)
    if current is None:
        return ""
    if isinstance(current, bool):
        if current:
            return "true"
        return "false"
    if isinstance(current, dict | list):
        return json.dumps(current, ensure_ascii=False)
    return str(current)


def _positive_id(value: str) -> bool:
    return value.isdecimal() and value != "0"
