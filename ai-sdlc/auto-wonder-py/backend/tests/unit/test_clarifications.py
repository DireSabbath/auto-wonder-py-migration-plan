"""澄清占位结果和路径。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.clarifications.schemas import PutClarificationRequest, empty_clarification
from autowonder.main import create_app


def test_missing_clarification_shape() -> None:
    """没有记录时返回工单 id、空正文和版本 0。"""
    view = empty_clarification(8)
    assert view.workitem_id == 8
    assert view.content_md is None
    assert view.version == 0
    assert view.gmt_modified is None
    body = PutClarificationRequest.model_validate({"contentMd": "结论"})
    assert body.content_md == "结论"
    assert PutClarificationRequest.model_validate({}).content_md is None


def test_clarification_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/workitems/{workitemId}/clarification" in paths
    response = client.get("/api/workitems/8/clarification")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
