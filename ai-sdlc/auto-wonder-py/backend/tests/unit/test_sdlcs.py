"""SDLC 契约中不依赖数据库的规则。"""

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data
from autowonder.main import create_app
from autowonder.sdlcs.checklist import validate_checklist
from autowonder.sdlcs.schemas import SdlcView, UpdateStepRequest
from autowonder.sdlcs.service import (
    build_in_use_message,
    chosen_step_order,
    merge_nullable_text,
    normalize_json,
    page_window,
    require_sdlc_name,
    require_valid_json,
    required_flag,
)


def test_sdlc_name_page_and_step_order() -> None:
    """空白名称拒绝，分页和序号回退与 Java 一致。"""
    assert require_sdlc_name("  研发流程  ") == "研发流程"
    try:
        require_sdlc_name(" ")
    except BizError as error:
        assert error.error_code == ErrorCode.SDLC_NAME_REQUIRED
    else:
        raise AssertionError("expected blank sdlc name")
    assert page_window(-1, 0) == (0, 20)
    assert page_window(3, 50) == (100, 50)
    assert chosen_step_order(None, 4) == 4
    assert chosen_step_order(0, 4) == 4
    assert chosen_step_order(2, 4) == 2
    assert required_flag(None) == 1
    assert required_flag(False) == 0


def test_checklist_definition_rules() -> None:
    """检查项必须是非空文本，或带条件的对象。"""
    validate_checklist(None)
    validate_checklist('["核对范围"]')
    validate_checklist(
        '[{"id":"scope","text":"核对范围","allowNotApplicable":true,"notApplicableWhen":"无界面"}]'
    )
    _expect_checklist("{}", "checklistJson 必须为数组")
    _expect_checklist("[1]", "checklistJson 第 1 项必须为非空文本或包含非空 id、text 的对象")
    _expect_checklist(
        '[{"id":"scope","text":"核对","allowNotApplicable":"yes"}]',
        "checklistJson 检查项 scope 的 allowNotApplicable 必须为布尔值",
    )
    _expect_checklist(
        '[{"id":"scope","text":"核对","allowNotApplicable":true}]',
        "checklistJson 检查项 scope 允许不适用时必须填写 notApplicableWhen 条件",
    )
    _expect_checklist(
        '["文本",{"id":"cl_0","text":"对象"}]',
        "checklistJson 检查项 id 重复: cl_0",
    )
    try:
        require_valid_json("gatePolicyJson", "{")
    except BizError as error:
        assert str(error) == "gatePolicyJson 不是合法的 JSON"
    else:
        raise AssertionError("expected invalid gate json")


def test_nullable_json_and_in_use_message() -> None:
    """空白 JSON 清空；删除占用说明带工单样例和数字员工。"""
    assert normalize_json("  ") is None
    assert normalize_json('["a"]') == '["a"]'
    assert merge_nullable_text(None, "保留") == "保留"
    assert merge_nullable_text("  ", "保留") is None
    message = build_in_use_message(6, [11, 12], ["审查(ID:9)"])
    assert message == (
        "流程被引用,无法删除: 引用源: 工单 6 个(#11, #12 等); "
        "数字员工 1 个(审查(ID:9))。请先解除上述引用后再删除。"
    )
    payload = dump_data(
        SdlcView(id=3, name="研发", is_default=0, status="DRAFT", step_count=0, steps=[])
    )
    assert payload["isDefault"] == 0
    assert payload["stepCount"] == 0
    assert payload["workType"] is None
    request = UpdateStepRequest.model_validate({"timeoutSeconds": None, "name": "分析"})
    assert "timeout_seconds" in request.model_fields_set
    assert "retry_budget" not in request.model_fields_set


def test_sdlc_routes_require_login() -> None:
    """未带令牌访问流程接口时返回 401。"""
    client = TestClient(create_app())
    response = client.get("/api/sdlcs")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"


def _expect_checklist(raw: str, message: str) -> None:
    try:
        validate_checklist(raw)
    except BizError as error:
        assert str(error) == message
    else:
        raise AssertionError(message)
