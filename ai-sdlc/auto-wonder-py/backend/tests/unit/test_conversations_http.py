"""会话 HTTP 的纯规则和路径。这些检查不访问数据库。"""

from datetime import datetime

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from autowonder.conversations.access import is_owner, read_allowed, write_error
from autowonder.conversations.commands import commands_from_snapshot
from autowonder.conversations.elicitation import normalize_elicitation_reply
from autowonder.conversations.prompt import render_system_prompt
from autowonder.conversations.router import clarification_router, platform_router
from autowonder.conversations.schemas import (
    PlatformConversationPatchRequest,
    PlatformTurnRequest,
)
from autowonder.conversations.service import (
    archive_moment,
    auto_title,
    belongs_to_workitem,
    normalize_keyword,
    normalize_page_size,
    normalize_user_title,
    page_offset,
    require_owner_user,
    should_apply_auto_title,
)
from autowonder.conversations.shares import share_target_invalid
from autowonder.conversations.turns import (
    canceled_reply_content,
    clarification_external_message_id,
    conversation_lock_name,
    dispatch_failure_summary,
    logical_lock_name,
    platform_external_message_id,
    platform_message_token,
    require_cancelable,
    require_selected_executor,
    resolve_processing,
)
from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app


def test_platform_title_page_and_archive_rules() -> None:
    """标题、分页和归档的合法输入按 Java 分支处理。"""
    assert normalize_user_title(None) is None
    assert normalize_user_title("   ") is None
    assert normalize_user_title("  发布计划  ") == "发布计划"
    assert normalize_user_title("题" * 255) == "题" * 255
    try:
        normalize_user_title("题" * 256)
    except BizError as error:
        assert error.error_code == ErrorCode.PLATFORM_CONVERSATION_TITLE_INVALID
    else:
        raise AssertionError("oversized title")
    assert auto_title("  a   b \n c  ") == "a b c"
    assert auto_title("字" * 31) == ("字" * 30) + "…"
    assert should_apply_auto_title("USER", None) is False
    assert should_apply_auto_title("AUTO", "已有") is False
    assert should_apply_auto_title(None, "   ") is True
    assert should_apply_auto_title(None, None) is True
    assert normalize_page_size(None) == 50
    assert normalize_page_size(0) == 50
    assert normalize_page_size(20) == 20
    assert normalize_page_size(500) == 200
    assert page_offset(None, 50) == 0
    assert page_offset(1, 50) == 0
    assert page_offset(0, 50) == 0
    assert page_offset(2, 50) == 50
    assert normalize_keyword(None) is None
    assert normalize_keyword("  ") is None
    assert normalize_keyword(" 管家 ") == "管家"
    moment = datetime(2026, 9, 24, 8, 0, 0)
    previous = datetime(2026, 1, 1, 0, 0, 0)
    assert archive_moment(None, previous, moment) == previous
    assert archive_moment(True, None, moment) == moment
    assert archive_moment(False, previous, moment) is None
    assert require_owner_user(7) == 7
    for owner in (None, 0, -1):
        try:
            require_owner_user(owner)
        except BizError as error:
            assert error.error_code == ErrorCode.UNAUTHORIZED
        else:
            raise AssertionError(owner)


def test_platform_access_and_share_rules() -> None:
    """读开放给 Owner 和 READ 分享，写只认 Owner。"""
    assert is_owner(None, 7) is False
    assert is_owner(7, 7) is True
    assert read_allowed("PLATFORM_ASSISTANT", None, 7, 7, None) is True
    assert read_allowed("PLATFORM_ASSISTANT", None, 7, 8, "READ") is True
    assert read_allowed("PLATFORM_ASSISTANT", None, 7, 9, None) is False
    assert read_allowed("WORKITEM_CLARIFICATION", None, 7, 7, None) is False
    assert read_allowed("PLATFORM_ASSISTANT", datetime(2026, 1, 1), 7, 7, None) is False
    assert write_error("PLATFORM_ASSISTANT", None, 7, 7, None) is None
    assert (
        write_error("PLATFORM_ASSISTANT", None, 7, 8, "READ")
        == ErrorCode.PLATFORM_CONVERSATION_OWNER_ONLY
    )
    hidden = ErrorCode.PLATFORM_CONVERSATION_NOT_FOUND_OR_NO_PERMISSION
    assert write_error("PLATFORM_ASSISTANT", None, 7, 9, None) == hidden
    assert write_error("DINGTALK", None, 7, 7, None) == hidden
    assert share_target_invalid(None, 7) is True
    assert share_target_invalid(0, 7) is True
    assert share_target_invalid(-1, 7) is True
    assert share_target_invalid(7, 7) is True
    assert share_target_invalid(8, 7) is False


def test_turn_identity_elicitation_and_command_rules() -> None:
    """幂等键、取消、卡片答案和命令快照。"""
    assert platform_message_token(None, "gen") == "gen"
    assert platform_message_token("  ", "gen") == "gen"
    assert platform_message_token("m1", "gen") == "m1"
    assert platform_external_message_id(22, "m1") == "web-platform:22:m1"
    assert clarification_external_message_id(None) == "web-clarification:null"
    assert clarification_external_message_id("") == "web-clarification:"
    assert clarification_external_message_id("abc") == "web-clarification:abc"
    assert logical_lock_name(10002, "PLATFORM_ASSISTANT", "ccid-1", 40013) == (
        "agent-conv-key:uAfh0mvnH6URGn6NQnlV9a1km_TGScoJxj3zpgjTg7Y"
    )
    assert conversation_lock_name(10002, 22) == "agent-conversation:10002:22"
    assert require_cancelable("IN", "QUEUED", 5) == "QUEUED"
    assert require_cancelable("IN", "PROCESSING", 5) == "PROCESSING"
    try:
        require_cancelable("OUT", "PROCESSING", 5)
    except BizError as error:
        assert error.error_code == ErrorCode.NOT_FOUND
        assert str(error) == "conversation turn not found: 5"
    else:
        raise AssertionError("outbound turn")
    try:
        require_cancelable("IN", "SUCCESS", 5)
    except BizError as error:
        assert error.error_code == ErrorCode.CONFLICT
        assert str(error) == "conversation turn is not cancelable: SUCCESS"
    else:
        raise AssertionError("terminal turn")
    try:
        require_selected_executor(None)
    except BizError as error:
        assert error.error_code == ErrorCode.SYSTEM_ERROR
    else:
        raise AssertionError("missing executor")
    assert require_selected_executor(9) == 9
    assert resolve_processing("PROCESSING", 3, "QUEUED", 4) == ("PROCESSING", 3)
    assert resolve_processing(None, None, "QUEUED", 4) == ("QUEUED", 4)
    assert resolve_processing(None, None, None, None) == (None, None)
    assert canceled_reply_content(None) == "响应已终止"
    assert canceled_reply_content("  已写一半  ") == "  已写一半  "
    summary = dispatch_failure_summary("a\nb", "RuntimeError")
    assert summary == "conversation dispatch failed: a b"
    assert len(dispatch_failure_summary("字" * 2000, "RuntimeError")) == 1024
    assert dispatch_failure_summary("  ", "RuntimeError") == (
        "conversation dispatch failed: RuntimeError"
    )
    assert normalize_elicitation_reply("decline", '{"kept":1}') is None
    assert normalize_elicitation_reply("accept", ' {"a": 1} ') == ' {"a": 1} '
    for action, content in (
        ("cancel", "{}"),
        ("accept", None),
        ("accept", " "),
        ("accept", "[]"),
        ("accept", "null"),
        ("accept", "1"),
        ("accept", "not-json"),
    ):
        try:
            normalize_elicitation_reply(action, content)
        except BizError as error:
            assert error.error_code == ErrorCode.PARAM_INVALID
        else:
            raise AssertionError((action, content))
    commands = commands_from_snapshot(
        '{"availableCommands":[{"name":"review","description":"看 diff","input":{"hint":"范围"}}]}'
    )
    assert commands[0].name == "review"
    assert commands[0].input is not None
    assert commands[0].input.hint == "范围"
    encoded = commands_from_snapshot('{"availableCommands":"[{\\"name\\":\\"plan\\"}]"}')
    assert encoded[0].name == "plan"
    assert commands_from_snapshot(None) == []
    assert commands_from_snapshot("  ") == []
    assert commands_from_snapshot("{") == []
    assert commands_from_snapshot('{"availableCommands":null}') == []
    prompt = render_system_prompt("管家", None, None, None, None, "PLATFORM_ASSISTANT", False)
    assert prompt.startswith("角色: 管家\n")
    assert "不能使用AskUserQuestion" in prompt
    assert "autowonder.propose_platform_actions" in prompt
    interactive = render_system_prompt(
        "澄清", None, None, None, None, "WORKITEM_CLARIFICATION", True
    )
    assert "不能使用AskUserQuestion" not in interactive
    assert "工单需求澄清会话" in interactive
    internal = render_system_prompt("内", None, None, None, None, "PLATFORM_INTERNAL", True)
    assert "不能使用AskUserQuestion" in internal
    try:
        render_system_prompt(None, None, None, None, None, "PLATFORM_ASSISTANT", True)
    except BizError as error:
        assert error.error_code == ErrorCode.SYSTEM_ERROR
    else:
        raise AssertionError("empty identity")
    assert belongs_to_workitem("WORKITEM_CLARIFICATION", "WORKITEM", 9, 9) is True
    assert belongs_to_workitem("PLATFORM_ASSISTANT", "WORKITEM", 9, 9) is False
    assert belongs_to_workitem("WORKITEM_CLARIFICATION", "WORKITEM", 9, 8) is False
    patch = PlatformConversationPatchRequest.model_validate({})
    assert patch.title is None
    assert patch.archived is None
    archived = PlatformConversationPatchRequest.model_validate({"archived": False, "title": "新"})
    assert archived.archived is False
    assert archived.title == "新"
    turn = PlatformTurnRequest.model_validate({"content": "hi", "clientMessageId": "m1"})
    assert turn.client_message_id == "m1"


def test_conversation_routes_match_java_and_require_login() -> None:
    """路径与 Java 一致。未登录是 401，业务码 10401。"""
    app = create_app()
    app.include_router(platform_router)
    app.include_router(clarification_router)
    client = TestClient(app)
    paths = client.app.openapi()["paths"]
    assert set(paths["/api/platform/conversations"]) == {"get", "post"}
    assert set(paths["/api/platform/conversations/{conversationId}"]) == {
        "get",
        "patch",
        "delete",
    }
    platform_tail = "/api/platform/conversations/{conversationId}"
    assert "post" in paths[platform_tail + "/commands/refresh"]
    assert "post" in paths[platform_tail + "/elicitations/{requestId}/reply"]
    assert "get" in paths[platform_tail + "/events"]
    assert set(paths[platform_tail + "/shares"]) == {"get", "post"}
    assert "delete" in paths[platform_tail + "/shares/{granteeUserId}"]
    assert "post" in paths[platform_tail + "/turns"]
    assert "post" in paths[platform_tail + "/turns/{turnId}/cancel"]
    assert "get" in paths[platform_tail + "/turns/{turnId}/events"]
    clarification = "/api/workitems/{workitemId}/clarification-conversations"
    assert set(paths[clarification]) == {"get", "post"}
    assert "get" in paths[clarification + "/{conversationId}"]
    assert "post" in paths[clarification + "/{conversationId}/commands/refresh"]
    assert "post" in paths[clarification + "/{conversationId}/elicitations/{requestId}/reply"]
    assert "get" in paths[clarification + "/{conversationId}/events"]
    assert "post" in paths[clarification + "/{conversationId}/turns"]
    assert "post" in paths[clarification + "/{conversationId}/turns/{turnId}/cancel"]
    assert "get" in paths[clarification + "/{conversationId}/turns/{turnId}/events"]
    listed = _access_text(platform_router, "/api/platform/conversations", "GET")
    assert "要求工作空间访问级别 READ_ONLY 才能查看平台管家对话。" in listed
    assert "READ_WRITE" not in listed
    created = _access_text(platform_router, "/api/platform/conversations", "POST")
    assert "READ_WRITE" in created
    assert "新建平台管家对话" in created
    assert "修改平台管家对话" in _access_text(
        platform_router, "/api/platform/conversations/{conversationId}", "PATCH"
    )
    assert "回答工单澄清问题卡片" in _access_text(
        clarification_router,
        clarification + "/{conversationId}/elicitations/{requestId}/reply",
        "POST",
    )
    response = client.get("/api/platform/conversations")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    missing = client.get("/api/workitems/9/clarification-conversations", params={"agentId": 1})
    assert missing.status_code == 401
    assert missing.json()["code"] == "10401"


def _access_text(router: object, path: str, method: str) -> str:
    texts: list[str] = []
    for route in getattr(router, "routes"):
        if not isinstance(route, APIRoute) or route.path != path:
            continue
        if method not in route.methods:
            continue
        for dependency in route.dependant.dependencies:
            doc = dependency.call.__doc__
            if doc is not None:
                texts.append(doc)
    return "\n".join(texts)
