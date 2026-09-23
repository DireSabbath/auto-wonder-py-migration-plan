"""工作空间契约中不依赖数据库的规则。"""

from datetime import datetime

from fastapi.testclient import TestClient

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.context import RequestContext, reset_context, set_context
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.page import PageResult
from autowonder.core.result import dump_data
from autowonder.main import create_app
from autowonder.workspaces.identity_tags import from_stored, normalize, to_json
from autowonder.workspaces.schemas import WorkspaceView
from autowonder.workspaces.service import exact_access_level, normalize_description, require_name


def test_identity_tags_normalize_and_json() -> None:
    """去重、去空白，并输出紧凑 JSON。"""
    assert normalize([" 研发 ", "研发", "", "产品"]) == ["研发", "产品"]
    assert to_json(None) == "[]"
    assert from_stored('["研发","产品"]') == ["研发", "产品"]
    assert from_stored(["研发"]) == ["研发"]
    assert from_stored(None) == []


def test_identity_tags_reject_bad_shapes() -> None:
    """超限、超长和坏 JSON 使用参数错误。"""
    try:
        normalize(["x" * 33])
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected tag length error")
    try:
        from_stored('{"a":1}')
    except BizError as error:
        assert str(error) == "Invalid persisted identity tags JSON"
    else:
        raise AssertionError("expected malformed json")


def test_workspace_name_rules() -> None:
    """空名、超长名和空白描述。"""
    assert require_name("  平台  ") == "平台"
    assert normalize_description("  ") is None
    try:
        require_name("   ")
    except BizError as error:
        assert error.error_code == ErrorCode.WORKSPACE_NAME_REQUIRED
    else:
        raise AssertionError("expected blank name error")
    try:
        exact_access_level("OWNER")
    except BizError as error:
        assert error.error_code == ErrorCode.WORKSPACE_ACCESS_LEVEL_INVALID
    else:
        raise AssertionError("expected invalid level")


def test_page_and_workspace_json_names() -> None:
    """分页和 isOwner 使用 Java 的字段名，时间写成毫秒。"""
    payload = dump_data(
        PageResult.model_validate(
            {
                "list": [WorkspaceView(id=1, name="平台", is_owner=True)],
                "total": 1,
                "pageNum": 1,
                "pageSize": 20,
            }
        )
    )
    assert payload["list"][0]["isOwner"] is True
    assert payload["pageNum"] == 1
    stamp = dump_data(datetime(2026, 9, 23, 8, 0, 0))
    assert isinstance(stamp, int)


def test_missing_workspace_is_not_member() -> None:
    """没有工作空间上下文时，访问级别依赖返回 11001，而不是级别不足。"""
    token = set_context(RequestContext(user_id=1))
    try:
        import asyncio

        checker = require_access(WorkspaceAccessLevel.READ_ONLY, "查看当前工作空间")
        try:
            asyncio.run(checker())
        except BizError as error:
            assert error.error_code == ErrorCode.WORKSPACE_NOT_MEMBER
        else:
            raise AssertionError("expected workspace membership error")
    finally:
        reset_context(token)


def test_workspace_routes_require_login() -> None:
    """未带令牌访问工作空间接口时返回 401。"""
    client = TestClient(create_app())
    response = client.get("/api/workspaces/mine")
    assert response.status_code == 401
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "10401"
