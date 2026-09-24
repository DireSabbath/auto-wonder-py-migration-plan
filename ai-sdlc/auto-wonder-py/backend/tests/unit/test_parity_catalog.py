"""端点清单与 Python 路由的覆盖，以及类前缀不再吞掉方法路径。"""

from scripts.extract_endpoints import endpoints_in
from verify.parity import (
    concrete_path,
    decode_body,
    load_catalog,
    missing_endpoints,
    normalize_document,
    normalize_path,
    python_route_keys,
)

_HEALTH = """
@RestController
public class HealthCheckController {
    @RequestMapping("/checkpreload.htm")
    public String getStatus() { return "success"; }

    @RequestMapping({"/status.taobao"})
    public String statusTaobao() { return "success"; }
}
"""

_AGENT = """
@RestController
@RequestMapping("/api/agents")
public class AgentController {
    @GetMapping("/{id}")
    public void detail() {}
}
"""


def test_method_mapping_is_not_a_class_prefix() -> None:
    rows = endpoints_in(_HEALTH, "HealthCheckController")
    assert {row["path"] for row in rows} == {"/checkpreload.htm", "/status.taobao"}


def test_class_prefix_joins_the_method_path() -> None:
    rows = endpoints_in(_AGENT, "AgentController")
    assert rows == [
        {
            "method": "GET",
            "path": "/api/agents/{id}",
            "controller": "AgentController",
        }
    ]


def test_normalize_document_drops_volatile_fields_and_stack_urls() -> None:
    normalized = normalize_document(
        {
            "id": 9,
            "traceId": "trace",
            "request_id": "req",
            "token": "secret",
            "gmtCreate": 1,
            "name": "保持",
            "mcpBaseUrl": "http://localhost:7001/api/mcp",
            "items": [{"id": 3, "updatedAt": 4, "label": "甲"}],
        }
    )
    assert normalized == {
        "name": "保持",
        "mcpBaseUrl": "{stack}/api/mcp",
        "items": [{"label": "甲"}],
    }


def test_concrete_path_replaces_every_parameter() -> None:
    assert concrete_path("/api/agents/{agentId}/executors/{id}") == "/api/agents/1/executors/1"


def test_decode_body_normalizes_json_and_keeps_empty() -> None:
    assert decode_body(b"", "application/json") is None
    assert decode_body(
        b'{"success":false,"code":"10401","request_id":"x"}',
        "application/json;charset=UTF-8",
    ) == {"success": False, "code": "10401"}
    assert decode_body(b"success", "application/json") == "success"


def test_python_routes_cover_the_java_catalog() -> None:
    catalog = load_catalog()
    missing = missing_endpoints(catalog, python_route_keys())
    assert missing == []
    assert len(catalog) == 375
    assert normalize_path("/api/workspaces/{id}") == normalize_path(
        "/api/workspaces/{workspace_id}"
    )
