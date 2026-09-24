"""状态模板字段规则和路径。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app
from autowonder.statemachines.schemas import UpdateTemplateRequest
from autowonder.statemachines.service import (
    apply_default_flag,
    kept_required_text,
    node_sort,
    require_present,
)


def test_status_template_field_rules() -> None:
    """空白名称拒绝；省略名称保留原值；只有显式默认才改默认标记。"""
    assert require_present("  需求  ", ErrorCode.STATUS_TEMPLATE_NAME_REQUIRED) == "需求"
    try:
        require_present(" ", ErrorCode.STATUS_TEMPLATE_WORK_TYPE_REQUIRED)
    except BizError as error:
        assert error.error_code == ErrorCode.STATUS_TEMPLATE_WORK_TYPE_REQUIRED
    else:
        raise AssertionError("expected blank work type")
    try:
        require_present(None, ErrorCode.STATUS_NODE_CODE_REQUIRED)
    except BizError as error:
        assert error.error_code == ErrorCode.STATUS_NODE_CODE_REQUIRED
    else:
        raise AssertionError("expected blank node code")
    assert kept_required_text(None, "原名") == "原名"
    assert kept_required_text(" 新名 ", "原名") == "新名"
    assert node_sort(None) == 0
    assert node_sort(3) == 3
    assert apply_default_flag(None, 0) == (False, 0)
    assert apply_default_flag(False, 1) == (False, 1)
    assert apply_default_flag(True, 0) == (True, 1)
    present = UpdateTemplateRequest.model_validate({"name": "甲", "isDefault": False})
    assert present.name == "甲"
    assert present.is_default is False
    omitted = UpdateTemplateRequest.model_validate({})
    assert omitted.name is None
    assert omitted.is_default is None


def test_status_template_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/status-templates" in paths
    assert "/api/status-templates/{id}" in paths
    assert "/api/status-templates/{id}/nodes" in paths
    assert "/api/status-templates/{id}/nodes/{nodeId}" in paths
    assert "/api/status-templates/{id}/transitions" in paths
    assert "/api/status-templates/{id}/transitions/{tid}" in paths
    response = client.get("/api/status-templates", params={"workType": "REQ"})
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
