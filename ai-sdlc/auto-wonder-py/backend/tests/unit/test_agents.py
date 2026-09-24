"""数字员工契约中不依赖数据库的规则。"""

from fastapi.testclient import TestClient

from autowonder.agents.branches import decode, encode, normalize
from autowonder.agents.evolution import (
    build_identity_map,
    evolution_mode_from,
    identity_text,
    identity_with_evolution_mode,
    parse_requested_evolution_mode,
)
from autowonder.agents.schemas import AgentView
from autowonder.agents.service import (
    field_provided,
    memory_ref_source,
    merge_field,
    page_window,
    platform_config_locked,
    platform_profile_locked,
    require_agent_name,
    version_fields_present,
)
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data
from autowonder.main import create_app


def test_agent_name_page_and_field_merge() -> None:
    """空白名称拒绝；分页、字段集合和角色字段是否改草稿与 Java 一致。"""
    assert require_agent_name("  审查  ") == "审查"
    try:
        require_agent_name(" ")
    except BizError as error:
        assert error.error_code == ErrorCode.AGENT_NAME_REQUIRED
    else:
        raise AssertionError("expected blank agent name")
    assert page_window(-1, 0) == (0, 20)
    assert page_window(2, 150) == (100, 100)
    assert field_provided(None, "name") is True
    assert field_provided(set(), "name") is False
    assert merge_field(None, "roleName", None, "保留") == "保留"
    assert merge_field(None, "roleName", "新角色", "保留") == "新角色"
    assert merge_field({"roleName"}, "roleName", None, "保留") is None
    assert merge_field(set(), "roleName", "新角色", "保留") == "保留"
    assert version_fields_present(None, None, None, None, None) is False
    assert version_fields_present(None, None, "角色", None, None) is True
    assert version_fields_present({"responsibilities"}, None, None, None, None) is True


def test_evolution_mode_and_identity_snapshot() -> None:
    """存量模式回落 ASSISTED；审核快照省略 null，并按字典序编码。"""
    assert evolution_mode_from(None) == "ASSISTED"
    assert evolution_mode_from(" manual ") == "MANUAL"
    assert evolution_mode_from("nope") == "ASSISTED"
    try:
        parse_requested_evolution_mode("nope")
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected invalid evolution mode")
    assert identity_with_evolution_mode('{"roleCode":"dev"}', " ") is None
    updated = identity_with_evolution_mode('{"roleCode":"dev"}', "assisted")
    assert updated == {"roleCode": "dev", "evolutionMode": "ASSISTED"}
    payload = build_identity_map(
        name="Ada",
        avatar_url=None,
        role_name="审查",
        role_code=None,
        business_background=None,
        responsibilities=None,
        identity='{"evolutionMode":"MANUAL"}',
    )
    assert "avatarUrl" not in payload
    assert payload["evolutionMode"] == "MANUAL"
    assert identity_text(payload) == '{"evolutionMode":"MANUAL","name":"Ada","roleName":"审查"}'


def test_branch_patterns_and_platform_locks() -> None:
    """分支规则只留一个结尾通配符；平台员工锁住名称和流程。"""
    assert encode(None) is None
    assert encode([]) is None
    assert encode(["main", "main", "release/*"]) == '["main","release/*"]'
    assert decode('["main","release/*"]') == ["main", "release/*"]
    assert normalize(["feature/board"]) == ["feature/board"]
    _expect_branch([""], "提交分支规则不能为空")
    _expect_branch([" main"], "提交分支规则不能包含首尾空白或控制字符:  main")
    _expect_branch(["a*b"], "提交分支规则只允许一个结尾通配符: a*b")
    _expect_branch(["bad..name"], "提交分支规则不是合法的 Git 分支: bad..name")
    try:
        decode("{")
    except BizError as error:
        assert str(error) == "仓库提交分支规则存储格式不合法"
    else:
        raise AssertionError("expected invalid stored branch patterns")
    assert platform_profile_locked("PLATFORM", None, "Ada", None) is True
    assert platform_profile_locked("PLATFORM", None, None, None) is False
    assert platform_profile_locked("STANDARD", None, "Ada", None) is False
    assert platform_config_locked("PLATFORM", 9) is True
    assert platform_config_locked("PLATFORM", None) is False
    card = dump_data(AgentView(id=4, name="Ada", kind="STANDARD", status="DRAFT", has_draft=False))
    assert card["hasDraft"] is False
    assert card["executorTotalCount"] == 0
    assert card["squadIds"] is None
    assert memory_ref_source(None) == "DIRECT"
    assert memory_ref_source("SQUAD") == "SQUAD_IMPORT"


def test_agent_routes_match_java_and_require_login() -> None:
    """路径名与 Java 目录一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/agents/reviews/count" in paths
    assert "/api/agents/{id}/versions/{versionNo}" in paths
    assert "/api/agents/{id}/environment-variables/{environmentVariableId}" in paths
    assert "/api/agents/{id}/repos/{repoId}" in paths
    assert "/api/agents/{id}/skills/{skillId}" in paths
    assert "/api/agents/{id}/memories/{memoryId}" in paths
    response = client.get("/api/agents")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"


def _expect_branch(patterns: list[str | None], message: str) -> None:
    try:
        normalize(patterns)
    except BizError as error:
        assert str(error) == message
    else:
        raise AssertionError(message)
