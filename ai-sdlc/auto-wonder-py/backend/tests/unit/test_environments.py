"""环境变量名称、说明和引用说明。这些规则不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data
from autowonder.environments.names import normalize_description, validate_name
from autowonder.environments.schemas import EnvironmentVariableView
from autowonder.environments.service import (
    VariableUse,
    describe_references,
    require_update_flag,
)
from autowonder.main import create_app


def test_environment_variable_name_and_description() -> None:
    """名称必须是标识符，保留名和超长说明按 Java 文案拒绝。"""
    assert validate_name("  api_token  ") == "api_token"
    assert validate_name("_A1") == "_A1"
    _expect_name("1token", ErrorCode.ENVIRONMENT_VARIABLE_NAME_INVALID)
    _expect_name("has-dash", ErrorCode.ENVIRONMENT_VARIABLE_NAME_INVALID)
    _expect_name("autowonder_token", ErrorCode.ENVIRONMENT_VARIABLE_NAME_RESERVED)
    _expect_name("CODEX_HOME", ErrorCode.ENVIRONMENT_VARIABLE_NAME_RESERVED)
    assert normalize_description(None) is None
    assert normalize_description("  ") is None
    assert normalize_description("  构建令牌  ") == "构建令牌"
    try:
        normalize_description("x" * 513)
    except BizError as error:
        assert str(error) == "环境变量说明不能超过512个字符"
    else:
        raise AssertionError("expected long description")
    assert require_update_flag(False) is False
    assert require_update_flag(True) is True
    try:
        require_update_flag(None)
    except BizError as error:
        assert str(error) == "updateValue 参数必须显式提供"
    else:
        raise AssertionError("expected missing updateValue")


def test_environment_variable_reference_message_and_mask() -> None:
    """删除占用说明带在线版本和编辑草稿，列表值固定脱敏。"""
    message = describe_references(
        [
            VariableUse(agent_id=7, agent_name="审查", version_no=3, ref_type="ONLINE"),
            VariableUse(agent_id=8, agent_name="  ", version_no=None, ref_type="EDITING"),
        ]
    )
    assert message == (
        "环境变量仍被数字员工引用,无法删除:"
        "审查(#7) 在线版本 v3；数字员工(#8) 编辑草稿"
        "。请先在对应草稿解除挂载并发布后再删除。"
    )
    payload = dump_data(EnvironmentVariableView(id=1, name="TOKEN", value="**", version=0))
    assert payload["value"] == "**"
    assert payload["gmtCreate"] is None


def test_environment_variable_routes_require_login() -> None:
    """路径与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/environment-variables" in paths
    assert "/api/environment-variables/{id}" in paths
    assert "/api/environment-variables/{id}/value" in paths
    response = client.get("/api/environment-variables")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"


def _expect_name(raw: str, code: ErrorCode) -> None:
    try:
        validate_name(raw)
    except BizError as error:
        assert error.error_code == code
    else:
        raise AssertionError(code.message)
