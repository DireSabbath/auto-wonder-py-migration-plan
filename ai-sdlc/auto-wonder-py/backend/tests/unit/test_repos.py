"""仓库名称、分页、字段出现和连接测试文案。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app
from autowonder.repos.connection import (
    build_ls_remote_command,
    probe_connection,
    sanitize_git_error,
)
from autowonder.repos.schemas import ConnectionTestRequest, repo_update_from_json
from autowonder.repos.service import (
    RepoUse,
    describe_active_refs,
    optional_field,
    page_window,
    require_repo_name,
    require_repo_url,
    required_field,
)


def test_repo_name_page_and_field_presence() -> None:
    """空白名称和地址拒绝；每页小于 1 时用 1；省略字段保留原值。"""
    assert require_repo_name("  平台  ") == "平台"
    assert require_repo_url("  git@example.com:a/b.git  ") == "git@example.com:a/b.git"
    _expect_name(" ")
    try:
        require_repo_url(None)
    except BizError as error:
        assert error.error_code == ErrorCode.REPO_URL_REQUIRED
    else:
        raise AssertionError("expected blank repo url")
    assert page_window(0, 0) == (0, 1)
    assert page_window(3, 150) == (200, 100)
    assert required_field(False, None, "原名", ErrorCode.REPO_NAME_REQUIRED) == "原名"
    assert required_field(True, " 新名 ", "原名", ErrorCode.REPO_NAME_REQUIRED) == "新名"
    try:
        required_field(True, " ", "原名", ErrorCode.REPO_NAME_REQUIRED)
    except BizError as error:
        assert error.error_code == ErrorCode.REPO_NAME_REQUIRED
    else:
        raise AssertionError("expected blank present name")
    assert optional_field(False, None, "main") == "main"
    assert optional_field(True, None, "main") is None
    fields = repo_update_from_json({"name": "仓库", "defaultBranch": None})
    assert fields.name_present is True
    assert fields.name == "仓库"
    assert fields.url_present is False
    assert fields.default_branch_present is True
    assert fields.default_branch is None
    assert repo_update_from_json(None).description_present is False


def test_repo_reference_message_and_connection_text() -> None:
    """删除占用说明和 git 失败文案与 Java 一致。"""
    message = describe_active_refs(
        [RepoUse(agent_id=4, agent_name="审查", version_no=2, ref_type="ONLINE")]
    )
    assert message == (
        "仓库仍被数字员工引用,无法删除:审查(#4) 在线版本 v2"
        "。请先在对应版本解除该仓库权限,或发布解除后的新版本再删除仓库。"
    )
    assert build_ls_remote_command("git", "https://example.com/a.git", "  ") == [
        "git",
        "ls-remote",
        "--heads",
        "https://example.com/a.git",
    ]
    assert build_ls_remote_command("git", "https://example.com/a.git", " main ") == [
        "git",
        "ls-remote",
        "--heads",
        "https://example.com/a.git",
        "main",
    ]
    assert sanitize_git_error(None) == "连接测试失败，请检查仓库地址、网络和本机 git 权限"
    assert sanitize_git_error("fatal: denied\n") == "连接测试失败：fatal: denied"
    warning = (
        "Warning: Permanently added 'example.com' (ED25519) to the list of known hosts.\n"
        "fatal: no\n"
    )
    assert sanitize_git_error(warning) == "连接测试失败：fatal: no"
    result = probe_connection(ConnectionTestRequest(url="  "))
    assert result.success is False
    assert result.message == "仓库地址不能为空"


def test_repo_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/repos/test-connection" in paths
    assert "/api/repos/relations" in paths
    assert "/api/repos/relations/{id}" in paths
    assert "/api/repos/{id}/conclusion" in paths
    assert "/api/repos/{id}/scan" in paths
    response = client.get("/api/repos")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"


def _expect_name(raw: str) -> None:
    try:
        require_repo_name(raw)
    except BizError as error:
        assert error.error_code == ErrorCode.REPO_NAME_REQUIRED
    else:
        raise AssertionError("expected blank repo name")
