"""记忆范围、审核计划和路径。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app
from autowonder.memories.schemas import CreateMemoryRequest, ReviewRequest
from autowonder.memories.sedimentation import first_line
from autowonder.memories.service import (
    artifact_source_ref,
    escape_like_wildcards,
    evolution_source_ref,
    group_key,
    manual_scope,
    mcp_existing_action,
    mcp_source_ref,
    normalize_scope,
    page_window,
    plan_review,
    require_decision,
    require_pending,
    require_title,
    scope_change,
    scope_change_comment,
    source_ref_text,
)


def test_memory_scope_title_and_like_escape() -> None:
    """组织范围清掉 owner；非法范围和空标题拒绝；LIKE 通配符加反斜杠。"""
    assert manual_scope("ORG", 1) == ("ORG", None)
    assert manual_scope(" agent ", 5) == ("AGENT", 5)
    assert normalize_scope("  ") is None
    assert require_title("  标题  ") == "标题"
    try:
        manual_scope("REPO", 9)
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected invalid scope")
    try:
        manual_scope("SQUAD", None)
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected missing owner")
    try:
        require_title("  ")
    except BizError as error:
        assert error.error_code == ErrorCode.MEMORY_TITLE_REQUIRED
    else:
        raise AssertionError("expected blank title")
    assert escape_like_wildcards(None) is None
    assert escape_like_wildcards("") == ""
    assert escape_like_wildcards("100%_a\\b") == "100\\%\\_a\\\\b"
    assert page_window(0, 500, 100) == (0, 100)
    assert page_window(0, 500, 50) == (0, 50)
    assert page_window(2, 10, 100) == (10, 10)
    assert group_key("AGENT", 30) == "AGENT:30"
    assert group_key("ORG", None) == "ORG:"


def test_memory_review_and_source_ref() -> None:
    """采纳可以提升范围；未采纳不能改范围；来源 JSON 与 Java 文本一致。"""
    kept = scope_change("AGENT", 30, "ADOPTED", "agent", 30)
    assert kept is None
    promoted = scope_change("AGENT", 30, "ADOPTED", "ORG", 999)
    assert promoted is not None
    assert promoted.scope == "ORG"
    assert promoted.owner_ref is None
    assert scope_change_comment("AGENT", 30, "ORG", None) == "范围变更: AGENT(30) -> ORG(null)"
    try:
        scope_change("AGENT", 30, "PENDING", "ORG", None)
    except BizError as error:
        assert error.error_code == ErrorCode.MEMORY_SCOPE_CHANGE_NOT_ADOPTED
    else:
        raise AssertionError("expected scope change on pending memory")
    try:
        scope_change("AGENT", 30, "ADOPTED", "SQUAD", None)
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected squad owner")
    adopted = plan_review("ADOPT", "verified", "SQUAD", 9, "AGENT", 30)
    assert adopted.status == "ADOPTED"
    assert adopted.content_md == "verified"
    assert adopted.promoted_scope == "SQUAD"
    assert adopted.promoted_owner == 9
    assert adopted.effective_scope == "SQUAD"
    assert adopted.effective_owner == 9
    workspace = plan_review("ADOPT", None, "ORG", None, "AGENT", 30)
    assert workspace.promoted_scope == "ORG"
    assert workspace.promoted_owner is None
    assert workspace.effective_owner is None
    same = plan_review("ADOPT", None, None, None, "AGENT", 30)
    assert same.promoted_scope is None
    assert same.effective_scope == "AGENT"
    assert same.effective_owner == 30
    rejected = plan_review("REJECT", "ignored", "GLOBAL", None, "AGENT", 30)
    assert rejected.status == "REJECTED"
    assert rejected.content_md is None
    assert rejected.promoted_scope is None
    try:
        plan_review("ADOPT", None, "SQUAD", None, "AGENT", 30)
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected owner on adopt")
    try:
        require_decision("ADOPTT")
    except BizError as error:
        assert str(error) == "decision 必须为 ADOPT 或 REJECT"
    else:
        raise AssertionError("expected invalid decision")
    try:
        require_pending("ADOPTED")
    except BizError as error:
        assert error.error_code == ErrorCode.MEMORY_NOT_PENDING
    else:
        raise AssertionError("expected pending memory")
    assert mcp_existing_action("PENDING", "旧", "旧正文", "新", "新正文") == "update"
    assert mcp_existing_action("ADOPTED", "原标题", "原正文", "原标题", "原正文") == "keep"
    assert mcp_existing_action("REJECTED", "原标题", "原正文", "复活", "新正文") == "reject"
    assert mcp_source_ref(99, 28559, 40014) == (
        '{"dispatchId":99,"workitemId":28559,"agentId":40014}'
    )
    assert evolution_source_ref(101) == '{"proposalId":101}'
    assert artifact_source_ref(42) == '{"artifactId":42}'
    assert artifact_source_ref(None) == "{}"
    assert source_ref_text(99) == "99"
    assert source_ref_text({"proposalId": 101}) == '{"proposalId":101}'
    assert first_line("第一行\n第二行") == "第一行"
    assert first_line("\n整段") == "整段"
    assert len(first_line("字" * 240)) == 200
    body = CreateMemoryRequest.model_validate(
        {"scope": "ORG", "ownerRef": 1, "title": "标题", "contentMd": "正文"}
    )
    assert body.owner_ref == 1
    review = ReviewRequest.model_validate({"decision": "ADOPT", "editedType": "FACT"})
    assert review.edited_type == "FACT"


def test_memory_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/memories" in paths
    assert "/api/memories/count" in paths
    assert "/api/memories/grouped" in paths
    assert "/api/memories/grouped/count" in paths
    assert "/api/memories/reviews" in paths
    assert "/api/memories/reviews/count" in paths
    assert "/api/memories/from-artifact" in paths
    assert "/api/memories/{id}" in paths
    assert "/api/memories/{id}/review" in paths
    response = client.get("/api/memories")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
