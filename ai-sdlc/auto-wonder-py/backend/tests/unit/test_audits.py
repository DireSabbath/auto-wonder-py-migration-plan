"""审计查询条件、操作人名称和路径。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.audits.service import (
    actor_type_of,
    compiled_filter_sql,
    detail_text,
    human_display_name,
    page_window,
)
from autowonder.main import create_app


def test_audit_page_actor_and_detail_text() -> None:
    """分页上限、昵称回落和细节文本与 AuditLogService 一致。"""
    assert page_window(0, 500) == (0, 100)
    assert page_window(2, 10) == (10, 10)
    assert page_window(1, 0) == (0, 1)
    assert human_display_name("  ", "ada") == "ada"
    assert human_display_name("艾达", "ada") == "艾达"
    assert human_display_name(None, "ada") == "ada"
    assert actor_type_of(None) is None
    assert actor_type_of("  ") is None
    assert actor_type_of("{") is None
    assert actor_type_of('{"actorType":"HUMAN"}') == "HUMAN"
    assert actor_type_of({"actorType": "AGENT"}) == "AGENT"
    assert actor_type_of({"actorType": True}) == "true"
    assert detail_text(None) is None
    assert detail_text('{"actorType":"HUMAN"}') == '{"actorType":"HUMAN"}'
    assert detail_text({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_audit_filters_match_java_search_condition() -> None:
    """空 actorType 不过滤；其余出现的条件与 MyBatis 片段一致。"""
    present = compiled_filter_sql(
        7,
        "repo",
        "UPDATE",
        "HUMAN",
        3,
        "repo",
        9,
        "2026-09-01 00:00:00",
        "2026-09-23 23:59:59",
        "100%",
    )
    normalized = " ".join(present.split()).replace("%%", "%")
    assert "audit_log.tenant_id = 7" in normalized
    assert "audit_log.module = 'repo'" in normalized
    assert "audit_log.action = 'UPDATE'" in normalized
    assert "json_valid" in normalized
    assert "json_extract" in normalized
    assert "json_unquote" in normalized
    assert "'$.actorType'" in normalized
    assert "= 'HUMAN'" in normalized
    assert "audit_log.actor_id = 3" in normalized
    assert "audit_log.target_type = 'repo'" in normalized
    assert "audit_log.target_id = 9" in normalized
    assert "gmt_create >= '2026-09-01 00:00:00'" in normalized
    assert "gmt_create <= '2026-09-23 23:59:59'" in normalized
    assert "detail_json LIKE '%100%%'" in normalized
    absent = compiled_filter_sql(7, None, None, "", None, None, None, None, None, None)
    where = absent.split("WHERE", 1)[1]
    assert "json_extract" not in where
    assert "module =" not in where
    assert "LIKE" not in where


def test_audit_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/audit-logs" in paths
    assert "/api/audit-logs/count" in paths
    response = client.get("/api/audit-logs")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
