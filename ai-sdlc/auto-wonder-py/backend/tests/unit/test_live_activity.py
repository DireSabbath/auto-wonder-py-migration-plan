"""实时活动投影与摘要脱敏。向量对齐 Java 的服务测试和清洗测试。"""

from datetime import UTC, datetime

from sqlalchemy.dialects import mysql

from autowonder.dispatch.action_text import looks_sensitive, sanitize, truncate
from autowonder.dispatch.live import (
    LiveActivityMetrics,
    LiveDispatch,
    action_status,
    action_type,
    event_bucket,
    events_after_seq_statement,
    is_displayable,
    label,
    normalize_limit,
    project_live,
    to_action,
    tool_action_type,
)
from autowonder.dispatch.trace import TraceSource


def test_sanitize_preserves_plain_text_and_rejects_null() -> None:
    """普通文本原样保留。空输入返回空。"""
    assert sanitize(None) is None
    assert sanitize("reading file src/Main.java") == "reading file src/Main.java"


def test_sanitize_redacts_signed_urls_and_keeps_expiry() -> None:
    """签名参数换成占位，过期时间仍可读。"""
    signed = "https://oss.example.com/file?Signature=abc123secret&Expires=9999"
    result = sanitize(signed)
    assert result is not None
    assert "abc123secret" not in result
    assert "[REDACTED]" in result
    assert "Expires=9999" in result
    urls = (
        "https://oss.aliyuncs.com/bucket/key?OSSAccessKeyId=LTAI123&Signature=abc%2Bdef",
        "https://s3.amazonaws.com/bucket?X-Amz-Signature=deadbeef1234",
        "https://cdn.example.com/f?access_token=longtoken123456789",
    )
    for url in urls:
        redacted = sanitize(url)
        assert redacted is not None
        assert "[REDACTED]" in redacted


def test_sanitize_redacts_bearer_assignment_and_opaque_tokens() -> None:
    """Bearer、密钥赋值和长随机串都不留原文。"""
    bearer = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123"
    bearer_result = sanitize(bearer)
    assert bearer_result is not None
    assert "eyJhbGciOiJIUzI1NiJ9" not in bearer_result
    assert "[REDACTED]" in bearer_result
    assigned = sanitize("apiKey=sk-proj-abcdefghijklmnop1234567890")
    assert assigned is not None
    assert "sk-proj-abcdefghijklmnop1234567890" not in assigned
    assert "[REDACTED]" in assigned
    opaque = "token a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0 found"
    opaque_result = sanitize(opaque)
    assert opaque_result is not None
    assert "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0" not in opaque_result


def test_sanitize_keeps_commit_sha_digest_and_source_path() -> None:
    """40 位提交号、64 位摘要和源码路径不是密钥。"""
    sha = "25371cb104ac019fb26674f0c495c410c01e5041"
    sha_result = sanitize("pushed commit " + sha + " to branch")
    assert sha_result is not None
    assert sha in sha_result
    assert "[REDACTED]" not in sha_result
    digest = "a" * 64
    digest_result = sanitize("digest " + digest)
    assert digest_result is not None
    assert digest in digest_result
    path = "repos/auto-wonder/src/main/java/com/aliyun/autowonder/util/RuntimeActionSanitizer.java"
    path_result = sanitize("edited " + path)
    assert path_result is not None
    assert path in path_result
    assert "[REDACTED]" not in path_result


def test_sanitize_redacts_padded_base64_and_strips_controls() -> None:
    """带等号的 Base64 仍脱敏。控制符换成空格。"""
    blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5QQ=="
    blob_result = sanitize("blob " + blob + " end")
    assert blob_result is not None
    assert blob not in blob_result
    assert "[REDACTED]" in blob_result
    controls = sanitize("hello\u0000world\u001b[31m")
    assert controls is not None
    assert "\u0000" not in controls
    assert "\u001b" not in controls


def test_sanitize_truncates_by_code_point() -> None:
    """超长文本先按码点预截断，再以省略号结尾。"""
    huge = sanitize("word " * 20_000, 50)
    assert huge is not None
    assert len(huge) <= 51
    assert huge.endswith("…")
    long_text = sanitize("word " * 60, 100)
    assert long_text is not None
    assert len(long_text) <= 101
    assert long_text.endswith("…")
    assert sanitize("abcdefghij", 20) == "abcdefghij"
    short = sanitize("abcdefghij", 5)
    assert short is not None
    assert short.endswith("…")


def test_looks_sensitive_and_truncate_follow_java() -> None:
    """敏感形状和截断边界与 Java 测试一致。"""
    assert looks_sensitive("https://x.com?Signature=abc123") is True
    assert looks_sensitive("Bearer abcdefghijklmnop") is True
    assert looks_sensitive("password=supersecret123") is True
    assert looks_sensitive("reading file Main.java") is False
    assert looks_sensitive(None) is False
    assert looks_sensitive("") is False
    assert looks_sensitive("   ") is False
    assert truncate(None, 100) is None
    assert truncate("hello", 10) == "hello"
    assert truncate("hello", 5) == "hello"
    assert truncate("hello world", 5) == "hello…"


def test_project_returns_newest_actions_for_a_dispatch() -> None:
    """两条可见事件都留下，最新的排在前面。"""
    events = [
        _event("step.started", 1, "开始编码"),
        _event("bash.completed", 2, "npm test passed"),
    ]
    activity = project_live(_dispatch(), events, None, None)
    assert activity.dispatch_id == 1
    assert activity.agent_id == 5
    assert activity.workitem_id == 1001
    assert activity.source_type == "WORKITEM"
    assert activity.attempt == 1
    assert activity.dispatch_status == "RUNNING"
    assert activity.schema_version == "1"
    assert activity.total_actions == 2
    assert activity.changed is True
    assert activity.awaiting_runtime is False
    assert activity.last_seq == 2
    assert activity.actions[0].summary == "npm test passed"
    assert activity.actions[0].event_type == "bash.completed"
    assert activity.current_action is not None
    assert activity.current_action.event_type == "step.started"
    assert activity.last_updated_at == activity.actions[0].event_time


def test_project_awaits_runtime_when_no_events() -> None:
    """一次完整读取没有任何可展示事件时，标记还在等待运行时。"""
    activity = project_live(_dispatch(), [], None, None)
    assert activity.awaiting_runtime is True
    assert activity.actions == []
    assert activity.changed is True
    assert activity.last_seq == 0


def test_project_filters_denied_event_types() -> None:
    """模型原文和思维链不进入活动，并按固定桶计数。"""
    metrics = LiveActivityMetrics()
    events = [
        _event("agent.message", 1, "secret model output"),
        _event("llm.completion", 2, "raw tokens"),
        _event("step.started", 3, "visible action"),
    ]
    activity = project_live(_dispatch(), events, None, None, metrics)
    assert activity.total_actions == 1
    assert activity.actions[0].summary == "visible action"
    assert metrics.filtered == ["agent_message", "llm"]


def test_project_after_seq_returns_only_newer_events() -> None:
    """游标之后的事件算一次回补。序号未前进时动作列表为空。"""
    metrics = LiveActivityMetrics()
    newer = project_live(_dispatch(), [_event("bash.started", 2, "new")], 1, None, metrics)
    assert newer.changed is True
    assert newer.total_actions == 1
    assert metrics.backfills == 1
    stalled = LiveActivityMetrics()
    empty = project_live(_dispatch(), [], 5, None, stalled)
    assert empty.changed is False
    assert empty.actions == []
    assert empty.awaiting_runtime is False
    assert empty.last_seq == 5
    assert stalled.backfills == 1


def test_project_skips_rows_at_or_before_cursor() -> None:
    """内存里序号不大于游标的行不投影，即使查询把它们带了回来。"""
    events = [
        _event("step.started", 1, "old"),
        _event("bash.started", 2, "new"),
    ]
    activity = project_live(_dispatch(), events, 1, None)
    assert activity.total_actions == 1
    assert activity.actions[0].summary == "new"


def test_project_respects_limit_and_marks_truncated() -> None:
    """窗口只留最新三条，总数仍是全部可展示动作。"""
    metrics = LiveActivityMetrics()
    events = [_event("step.progress", index, "event " + str(index)) for index in range(1, 11)]
    activity = project_live(_dispatch(), events, None, 3, metrics)
    assert activity.truncated is True
    assert len(activity.actions) == 3
    assert activity.total_actions == 10
    assert activity.actions[0].summary == "event 10"
    assert activity.actions[1].summary == "event 9"
    assert activity.actions[2].summary == "event 8"
    assert metrics.truncated_marks == [7]


def test_action_type_and_tool_categories() -> None:
    """事件前缀和工具名收成稳定类别。拒绝的类型没有类别。"""
    assert action_type("package.loaded", {}) == "CONTEXT_PREPARE"
    assert action_type("repo.cloned", {}) == "REPO_PREPARE"
    assert action_type("step.started", {}) == "SDLC_STEP"
    assert action_type("bash.completed", {}) == "COMMAND"
    assert action_type("mcp.tool_called", {}) == "MCP_CALL"
    assert action_type("artifact.uploaded", {}) == "ARTIFACT"
    assert action_type("session.started", {}) == "SESSION"
    assert action_type("turn.completed", {}) == "MODEL_TURN"
    assert action_type("subagent.spawned", {}) == "SUBAGENT"
    assert action_type("dispatch.started", {}) == "DISPATCH"
    assert action_type("agent.message", {}) is None
    assert action_type("llm.raw", {}) is None
    assert action_type("guidance.human", {}) is None
    assert action_type(None, {}) is None
    assert tool_action_type("Read") == "SEARCH_READ"
    assert tool_action_type("grep") == "SEARCH_READ"
    assert tool_action_type("Edit") == "FILE_EDIT"
    assert tool_action_type("Write") == "FILE_EDIT"
    assert tool_action_type("Bash") == "COMMAND"
    assert tool_action_type("mcp__autowonder__get_workitem") == "MCP_CALL"
    assert tool_action_type("Skill") == "SKILL"
    assert tool_action_type("Agent") == "SUBAGENT"
    assert tool_action_type("UnknownTool") == "TOOL"
    assert tool_action_type(None) == "TOOL"


def test_action_status_and_display_filter() -> None:
    """错误列优先。后缀和详情状态决定其余状态。"""
    assert action_status("bash.failed", {}, "exit 1") == "FAILED"
    assert action_status("step.failed", {}, None) == "FAILED"
    assert action_status("step.started", {}, None) == "RUNNING"
    assert action_status("bash.completed", {}, None) == "COMPLETED"
    assert action_status("session.interrupted", {}, None) == "PAUSED"
    assert action_status("session.resumed", {}, None) == "RESUMED"
    assert action_status("step.cancelled", {}, None) == "CANCELLED"
    assert action_status("step.progress", {}, None) == "RUNNING"
    assert action_status("repo.status", {}, None) == "INFO"
    assert action_status("step.progress", {"status": "failed"}, None) == "FAILED"
    assert action_status("step.progress", {"status": "paused"}, None) == "PAUSED"
    assert is_displayable("step.started") is True
    assert is_displayable("bash.completed") is True
    assert is_displayable("completion_requested") is True
    assert is_displayable("agent.message") is False
    assert is_displayable("llm.completion") is False
    assert is_displayable("guidance.human") is False
    assert is_displayable("comment.added") is False
    assert is_displayable(None) is False
    assert is_displayable("") is False
    assert is_displayable("agent.thinking") is False
    assert is_displayable("turn.reasoning") is False
    assert is_displayable("agent.chain_of_thought") is False
    assert action_type("agent.thinking", {}) is None
    assert is_displayable("turn.completed") is True


def test_handoff_and_labels() -> None:
    """交接事件可展示，中文短句与 Java 一致。"""
    assert is_displayable("handoff.submitted") is True
    assert action_type("handoff.submitted", {}) == "HANDOFF"
    assert label("HANDOFF", "handoff.submitted") == "已提交交接"
    assert label("HANDOFF", "handoff.accepted") == "交接已接收"
    assert label("HANDOFF", "handoff.unknown") == "交接处理"
    assert label("CONTEXT_PREPARE", "package.loaded") == "准备任务包与上下文"
    assert label("REPO_PREPARE", "repo.cloned") == "准备工作仓库"
    assert label("COMMAND", "bash.started") == "执行命令"
    assert label("DISPATCH", "dispatch.started") == "开始执行"
    assert label("SEARCH_READ", "agent.tool_use") == "检索或读取代码"


def test_loaded_capabilities_are_completed_preparation() -> None:
    """已加载是完成态准备。调用和工具使用仍是进行中。"""
    cases = (
        ("skill.loaded", "SKILL_LOAD", "已加载 Skill", "COMPLETED"),
        ("plugin.loaded", "PLUGIN_LOAD", "已加载 Plugin", "COMPLETED"),
        ("mcp.loaded", "MCP_LOAD", "已加载 MCP 服务", "COMPLETED"),
        ("skill.invoked", "SKILL", "调用 Skill", "RUNNING"),
        ("mcp.call", "MCP_CALL", "调用 MCP 工具", "RUNNING"),
    )
    for event_type, kind, caption, status in cases:
        event = _event(event_type, 1, None)
        event.detail_json = '{"name":"example"}'
        action = to_action(_dispatch(), event)
        assert action is not None
        assert action.action_type == kind
        assert action.summary == caption + " · example"
        assert action.status == status
    skill = _event("agent.tool_use", 2, None)
    skill.detail_json = '{"tool":"Skill"}'
    skill_action = to_action(_dispatch(), skill)
    assert skill_action is not None
    assert skill_action.action_type == "SKILL"
    assert skill_action.status == "RUNNING"
    mcp = _event("agent.tool_use", 2, None)
    mcp.detail_json = '{"tool":"mcp__autowonder__get_workitem"}'
    mcp_action = to_action(_dispatch(), mcp)
    assert mcp_action is not None
    assert mcp_action.action_type == "MCP_CALL"
    assert mcp_action.status == "RUNNING"


def test_to_action_hides_denied_events_and_secrets() -> None:
    """模型原文没有动作。命令摘要里的令牌回退成中文短句。"""
    assert to_action(_dispatch(), _event("agent.message", 1, "secret")) is None
    secret = "token=sk-abcdefghijklmnopqrstuvwxyz123456"
    action = to_action(_dispatch(), _event("bash.completed", 1, secret))
    assert action is not None
    assert action.summary is not None
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in action.summary
    assert action.summary == "执行命令"


def test_event_buckets_stay_fixed() -> None:
    """过滤指标只使用固定桶。"""
    assert event_bucket("llm.completion") == "llm"
    assert event_bucket("guidance.human") == "guidance"
    assert event_bucket("comment.added") == "comment"
    assert event_bucket("agent.message") == "agent_message"
    assert event_bucket("agent.thinking") == "reasoning"
    assert event_bucket("turn.reasoning") == "reasoning"
    assert event_bucket("agent.chain_of_thought") == "reasoning"
    assert event_bucket("step.started") == "other"
    assert event_bucket("some.brand.new.event.type") == "other"
    assert event_bucket(None) == "none"
    assert event_bucket("") == "none"
    assert event_bucket("   ") == "none"
    assert event_bucket("LLM.Raw") == "llm"
    assert event_bucket("Agent.Message") == "agent_message"


def test_normalize_limit_uses_java_defaults() -> None:
    """缺省和小于等于 0 用 50，超过 200 截断。"""
    assert normalize_limit(None) == 50
    assert normalize_limit(0) == 50
    assert normalize_limit(-3) == 50
    assert normalize_limit(201) == 200
    assert normalize_limit(3) == 3


def test_events_after_seq_keep_null_seq_and_java_order() -> None:
    """游标查询包含空序号，并按序号、主键排序。"""
    compiled = events_after_seq_statement(1, 44, 5).compile(
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "dispatch_runtime_event.tenant_id = 1" in sql
    assert "dispatch_runtime_event.dispatch_id = 44" in sql
    assert "dispatch_runtime_event.seq IS NULL" in sql
    assert "dispatch_runtime_event.seq > 5" in sql
    assert "coalesce(dispatch_runtime_event.seq, dispatch_runtime_event.id)" in sql
    assert "dispatch_runtime_event.id ASC" in sql


def _dispatch() -> LiveDispatch:
    return LiveDispatch(
        id=1,
        agent_id=5,
        workitem_id=1001,
        source_type="WORKITEM",
        attempt=1,
        status="RUNNING",
    )


def _event(event_type: str, seq: int, message: str | None) -> TraceSource:
    return TraceSource(
        event_id="evt-" + str(seq),
        seq=seq,
        event_type=event_type,
        message=message,
        event_time=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
        step_name="编码实现",
    )
