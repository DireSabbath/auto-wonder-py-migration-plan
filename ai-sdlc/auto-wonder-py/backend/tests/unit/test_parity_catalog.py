"""端点清单与 Python 路由的覆盖，以及类前缀不再吞掉方法路径。"""

from scripts.extract_endpoints import endpoints_in
from verify.parity import load_catalog, missing_endpoints, normalize_path, python_route_keys

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


def test_python_routes_cover_the_java_catalog() -> None:
    catalog = load_catalog()
    missing = missing_endpoints(catalog, python_route_keys())
    assert missing == []
    assert len(catalog) == 375
    assert normalize_path("/api/workspaces/{id}") == normalize_path(
        "/api/workspaces/{workspace_id}"
    )
