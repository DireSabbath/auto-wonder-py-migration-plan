"""运行轨迹投影、活动合并和完成态大纲。这些检查不访问数据库。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.artifacts.models import Artifact
from autowonder.core.errors import BizError, ErrorCode, IllegalArgumentError
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.query import require_same_tenant
from autowonder.dispatch.schemas import (
    RuntimeActivity,
    RuntimeActivityTimeline,
    RuntimeSpan,
    RuntimeTrace,
    RuntimeTurn,
)
from autowonder.dispatch.trace import (
    TraceSource,
    choose_published_trace,
    events_by_arrival_statement,
    events_by_seq_statement,
    project_activities,
    project_trace,
)
from autowonder.dispatch.trace_artifact import (
    artifacts_for_dispatch,
    find_turn,
    name_matches,
    outline_from_bytes,
    require_named_artifact,
    require_observation,
    trace_from_bytes,
    validate_content_ref,
)
from autowonder.main import create_app

_TRACE_DOCUMENT = (
    '{"schemaVersion":"autowonder.runtime-trace.v2","dispatchId":"44",'
    '"sessions":[{"sessionId":"qoder-session","provider":"qoder","turns":[{'
    '"traceId":"turn-1","systemPrompt":"full system prompt",'
    '"prompt":"full user prompt","contextFiles":[{"role":"CONTEXT",'
    '"name":"issue_context.md","contentRef":"context/files/abc",'
    '"previewable":true}],"observations":[{"observationId":"turn-1:agent",'
    '"type":"AGENT","name":"qoder turn","children":[{'
    '"observationId":"call-1","parentObservationId":"turn-1:agent",'
    '"type":"MCP","name":"code.search","input":{"query":"posterior"},'
    '"output":"result payload","durationMs":283}]}]}]}]}'
)


def test_projects_one_bash_call_into_session_turn_and_span() -> None:
    """单条 bash 事件展开成会话、回合和区间，不另建轨迹表。"""
    source = _event(
        7,
        "bash.call",
        None,
        runtimeId="rt1",
        provider="codex",
        sessionId="s1",
        turnId="t1",
        spanId="span1",
    )
    source.event_id = "44:7"
    source.seq = 7
    trace = project_trace(44, [source], None)
    assert trace.dispatch_id == 44
    assert trace.events[0].event_id == "44:7"
    assert trace.events[0].detail["turnId"] == "t1"
    assert trace.runtime_id == "rt1"
    assert trace.sessions[0].session_id == "s1"
    assert trace.sessions[0].turns[0].turn_id == "t1"
    assert trace.sessions[0].turns[0].spans[0].span_id == "span1"


def test_folds_sessions_turns_tools_and_usage_once() -> None:
    """用量按回合汇总到会话和轨迹，总数只算输入加输出。"""
    sources = [
        _event(1, "step.started", _at(0), stepId="implementation", stepName="Implementation"),
        _event(
            2,
            "session.started",
            _at(1),
            runtimeId="rt1",
            provider="codex",
            sessionId="s1",
        ),
        _event(3, "turn.started", _at(2), sessionId="s1", turnId="t1", spanId="t1:llm"),
        _event(
            4,
            "llm.started",
            _at(2),
            sessionId="s1",
            turnId="t1",
            spanId="t1:llm",
            model="gpt-5",
        ),
        _event(
            5,
            "bash.call",
            _at(3),
            sessionId="s1",
            turnId="t1",
            spanId="call-1",
            callId="call-1",
            tool="bash",
            inputSummary="pnpm test",
        ),
        _event(
            6,
            "bash.result",
            _at(5),
            sessionId="s1",
            turnId="t1",
            spanId="call-1",
            callId="call-1",
            tool="bash",
            status="ok",
            durationMs=1500,
            outputSummary="26 passed",
        ),
        _event(
            7,
            "llm.usage",
            _at(6),
            sessionId="s1",
            turnId="t1",
            spanId="t1:llm",
            model="gpt-5",
            inputTokens=1200,
            outputTokens=300,
            reasoningTokens=80,
            cacheReadTokens=400,
        ),
        _event(
            8,
            "llm.completed",
            _at(7),
            sessionId="s1",
            turnId="t1",
            spanId="t1:llm",
            durationMs=5000,
        ),
        _event(
            9,
            "turn.completed",
            _at(8),
            sessionId="s1",
            turnId="t1",
            spanId="t1:llm",
            durationMs=6000,
        ),
        _event(
            10,
            "session.interrupted",
            _at(9),
            sessionId="s1",
            reason="paused",
            checkpointSeq=18,
        ),
        _event(11, "session.resumed", _at(60), sessionId="s1", mode="resume"),
        _event(12, "turn.started", _at(61), sessionId="s1", turnId="t2", spanId="t2:llm"),
        _event(
            13,
            "llm.usage",
            _at(64),
            sessionId="s1",
            turnId="t2",
            spanId="t2:llm",
            inputTokens=500,
            outputTokens=100,
        ),
        _event(
            14,
            "turn.interrupted",
            _at(65),
            sessionId="s1",
            turnId="t2",
            spanId="t2:llm",
            durationMs=4000,
        ),
        _event(15, "session.interrupted", _at(66), sessionId="s1", reason="paused"),
    ]
    trace = project_trace(45, sources, None)
    assert trace.changed is True
    assert trace.last_seq == 15
    assert trace.token_usage.total_tokens == 2100
    assert trace.token_usage.available is True
    assert trace.token_usage.input_tokens == 1700
    assert trace.token_usage.output_tokens == 400
    assert trace.token_usage.reasoning_tokens == 80
    session = trace.sessions[0]
    assert session.status == "INTERRUPTED"
    assert len(session.turns) == 2
    assert session.duration_ms == 65_000
    assert session.token_usage.total_tokens == 2100
    first = session.turns[0]
    assert first.step_id == "implementation"
    assert first.step_name == "Implementation"
    assert first.status == "COMPLETED"
    assert first.duration_ms == 6000
    assert first.token_usage.total_tokens == 1500
    bash = _span(first, "BASH")
    assert bash.status == "COMPLETED"
    assert bash.duration_ms == 1500
    assert bash.input_summary == "pnpm test"
    assert bash.output_summary == "26 passed"
    interrupted = [item for item in session.boundaries if item.kind == "INTERRUPTED"]
    resumed = [item for item in session.boundaries if item.kind == "RESUMED"]
    assert len(interrupted) == 2
    assert len(resumed) == 1
    assert interrupted[0].label == "INTERRUPTED · paused · checkpoint #18"
    assert interrupted[1].label == "INTERRUPTED · paused"


def test_keeps_prompts_payloads_and_marks_missing_usage_unavailable() -> None:
    """提示词和工具载荷原样保留。没有 usage 事件时用量不可用。"""
    sources = [
        _event(1, "session.started", _at(0), sessionId="qoder-session", provider="qoder"),
        _event(
            2,
            "turn.started",
            _at(1),
            sessionId="qoder-session",
            turnId="turn-1",
            prompt="full user prompt",
            systemPrompt="full system prompt",
        ),
        _event(
            3,
            "bash.call",
            _at(2),
            sessionId="qoder-session",
            turnId="turn-1",
            spanId="call-1",
            tool="Bash",
            input={"command": "pwd", "Authorization": "Bearer raw"},
        ),
        _event(
            4,
            "bash.result",
            _at(3),
            sessionId="qoder-session",
            turnId="turn-1",
            spanId="call-1",
            tool="Bash",
            status="completed",
            output="/workspace\n",
            durationMs=1000,
        ),
        _event(
            5,
            "llm.started",
            _at(1),
            sessionId="qoder-session",
            turnId="turn-1",
            spanId="turn-1:llm",
            model="qmodel_latest",
        ),
        _event(
            6,
            "agent.message",
            _at(3),
            providerEvent=True,
            sessionId="qoder-session",
            turnId="turn-1",
            spanId="turn-1:llm",
            content="hello ",
        ),
        _event(
            7,
            "agent.message",
            _at(3),
            providerEvent=True,
            sessionId="qoder-session",
            turnId="turn-1",
            spanId="turn-1:llm",
            content="world",
        ),
        _event(
            8,
            "agent.tool_use",
            _at(3),
            sessionId="qoder-session",
            turnId="turn-1",
            spanId="skill-1",
            tool="Skill",
            input={"skill": "verify", "args": "run tests"},
        ),
        _event(
            9,
            "agent.tool_result",
            _at(4),
            sessionId="qoder-session",
            turnId="turn-1",
            spanId="skill-1",
            tool="Skill",
            status="completed",
            output="done",
            durationMs=900,
        ),
        _event(
            10,
            "agent.message",
            _at(4),
            sessionId="qoder-session",
            turnId="turn-1",
            spanId="internal",
            content="runtime diagnostic",
        ),
        _event(
            11,
            "turn.completed",
            _at(4),
            sessionId="qoder-session",
            turnId="turn-1",
            durationMs=3000,
        ),
    ]
    trace = project_trace(48, sources, None)
    turn = trace.sessions[0].turns[0]
    assert trace.sessions[0].duration_ms == 4_000
    assert turn.prompt == "full user prompt"
    assert turn.system_prompt == "full system prompt"
    assert turn.token_usage.available is False
    assert trace.token_usage.available is False
    bash = _span(turn, "BASH")
    assert bash.input["command"] == "pwd"
    assert bash.input["Authorization"] == "Bearer raw"
    assert bash.output == "/workspace\n"
    provider = _span(turn, "PROVIDER")
    assert provider.content == "hello world"
    assert len([item for item in turn.spans if item.kind == "PROVIDER"]) == 1
    skill = _span(turn, "SKILL")
    assert skill.name == "verify"
    assert skill.duration_ms == 900
    assert skill.output == "done"


def test_unchanged_after_known_sequence_omits_events() -> None:
    """已知序号不小于末序号时，changed 为假，事件和会话都空。"""
    source = _event(4, "session.started", _at(0), sessionId="s1")
    trace = project_trace(46, [source], 4)
    assert trace.changed is False
    assert trace.last_seq == 4
    assert trace.events == []
    assert trace.sessions == []


def test_resume_of_same_session_is_not_a_fork() -> None:
    """父会话等于自己时清空父 id，状态保持运行。"""
    source = _event(
        1,
        "session.resumed",
        _at(0),
        sessionId="s1",
        parentSessionId="s1",
    )
    trace = project_trace(47, [source], None)
    assert len(trace.sessions) == 1
    assert trace.sessions[0].parent_session_id is None
    assert trace.sessions[0].status == "RUNNING"
    assert trace.sessions[0].boundaries[0].kind == "RESUMED"


def test_trace_event_time_stays_empty_when_only_creation_time_exists() -> None:
    """轨迹事件时间只看来源 eventTime，不用创建时间填。"""
    source = _event(1, "session.started", _at(0), sessionId="s1")
    source.event_time = None
    source.gmt_create = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    trace = project_trace(56, [source], None)
    assert trace.events[0].event_time is None


def test_merges_one_stream_and_drops_tool_input() -> None:
    """同一流的连续消息拼在一起。工具调用会切开，且不展示原始输入。"""
    first = _event(
        1,
        "agent.message",
        _at(0),
        sessionId="s1",
        turnId="t1",
        spanId="llm",
        content="hello ",
    )
    first.gmt_create = datetime(2026, 7, 30, 10, 10, tzinfo=UTC)
    second = _event(2, "agent.message", _at(1), sessionId="s1", turnId="t1", spanId="llm")
    second.message = "world"
    after = _event(
        4,
        "agent.message",
        _at(3),
        sessionId="s1",
        turnId="t1",
        spanId="llm",
        content="after tool",
    )
    after.event_time = None
    after.gmt_create = datetime(2026, 7, 30, 10, 0, 4, tzinfo=UTC)
    timeline = project_activities(49, [first, second, _tool_call(), after])
    assert len(timeline.activities) == 2
    merged = timeline.activities[0]
    assert merged.seq == 1
    assert merged.event_time == "2026-07-30T10:00:00Z"
    assert merged.event_type == "agent.message"
    assert merged.level == "INFO"
    assert merged.content == "hello world"
    assert timeline.activities[1].event_time == "2026-07-30T10:00:04Z"
    assert timeline.activities[1].content == "after tool"
    assert all("Bearer raw" not in (item.content or "") for item in timeline.activities)
    assert "changed" not in RuntimeActivityTimeline.model_fields
    assert "last_seq" not in RuntimeActivityTimeline.model_fields
    assert "detail_json" not in RuntimeActivity.model_fields
    assert "input" not in RuntimeActivity.model_fields


def test_stored_error_beats_failure_detail() -> None:
    """失败活动优先用已落库的 error 列。"""
    failed = _event(
        1,
        "step.failed",
        _at(0),
        reason="detail failure reason",
        error="detail error",
        message="detail message",
    )
    failed.error = "stored error"
    failed.message = "stored message"
    timeline = project_activities(50, [failed])
    assert len(timeline.activities) == 1
    assert timeline.activities[0].level == "ERROR"
    assert timeline.activities[0].event_type == "step.failed"
    assert timeline.activities[0].content == "stored error"


def test_failure_fallbacks_skip_textless_and_non_failure_reasons() -> None:
    """失败文案按列和 detail 回退。没有文本的失败，以及非失败原因，都不出现。"""
    reason = _event(1, "step.failed", _at(0), reason="reason fallback")
    error = _event(2, "session.failed", _at(1), error="error fallback")
    message = _event(3, "dispatch.failed", _at(2))
    message.message = "stored message fallback"
    textless = _event(4, "session.failed", _at(3))
    paused = _event(5, "session.interrupted", _at(4), reason="paused")
    timeline = project_activities(51, [reason, error, message, textless, paused])
    assert [item.content for item in timeline.activities] == [
        "reason fallback",
        "error fallback",
        "stored message fallback",
    ]
    assert all(item.level == "ERROR" for item in timeline.activities)


def test_null_sequence_failover_stays_between_messages() -> None:
    """序号为空的失败仍按到达顺序夹在两条消息之间。"""
    first = _event(
        10,
        "agent.message",
        _at(0),
        sessionId="s1",
        turnId="t1",
        spanId="llm",
        content="first message",
    )
    failover = _event(0, "dispatch.executor_failover", _at(1))
    failover.seq = None
    failover.error = "Runtime switching failed"
    second = _event(
        11,
        "agent.message",
        _at(2),
        sessionId="s1",
        turnId="t1",
        spanId="llm",
        content="second message",
    )
    first_snapshot = project_activities(52, [first])
    second_snapshot = project_activities(52, [first, failover, second])
    assert [item.content for item in first_snapshot.activities] == ["first message"]
    assert [item.content for item in second_snapshot.activities] == [
        "first message",
        "Runtime switching failed",
        "second message",
    ]
    assert second_snapshot.activities[1].level == "ERROR"


def test_structured_detail_values_do_not_become_activities() -> None:
    """对象和数组不能进活动正文，避免把载荷漏出去。"""
    message = _event(
        1,
        "agent.message",
        _at(0),
        content={"input": {"token": "Bearer raw"}},
    )
    failed = _event(2, "step.failed", _at(1), reason={"prompt": "full secret prompt"})
    timeline = project_activities(53, [message, failed])
    assert timeline.activities == []


def test_json_strings_are_rejected_and_plain_text_stays() -> None:
    """看起来像 JSON 对象或数组的字符串丢掉，普通文本留下。"""
    stored_json = _event(1, "agent.message", _at(0))
    stored_json.message = '{"token":"stored message leak"}'
    detail_json = _event(2, "agent.message", _at(1), content='["detail message leak"]')
    structured = _event(
        3,
        "step.failed",
        _at(2),
        reason='{"reason":"detail reason leak"}',
        error='["detail error leak"]',
        message='{"message":"detail message leak"}',
    )
    structured.error = '{"error":"stored error leak"}'
    structured.message = '["stored message leak"]'
    plain_message = _event(4, "agent.message", _at(3))
    plain_message.message = "plain stored message"
    plain_detail = _event(5, "agent.message", _at(4), content="plain detail message")
    plain_failure = _event(6, "step.failed", _at(5))
    plain_failure.error = "plain stored error"
    timeline = project_activities(
        63,
        [
            stored_json,
            detail_json,
            structured,
            plain_message,
            plain_detail,
            plain_failure,
        ],
    )
    assert [item.content for item in timeline.activities] == [
        "plain stored message",
        "plain detail message",
        "plain stored error",
    ]
    assert all("leak" not in (item.content or "") for item in timeline.activities)


def test_reload_uses_the_updated_row() -> None:
    """同一事件被更新后，下一次投影用新内容，失败级别改为 ERROR。"""
    before = _event(1, "agent.message", _at(0), content="first detail")
    before.event_id = "64:1"
    before.message = "first message"
    after = _event(1, "agent.message", _at(0), content="updated detail")
    after.event_id = "64:1"
    after.message = "updated message"
    after.error = "updated error"
    first_snapshot = project_activities(64, [before])
    second_snapshot = project_activities(64, [after])
    assert first_snapshot.activities[0].content == "first detail"
    assert second_snapshot.activities[0].content == "updated error"
    assert second_snapshot.activities[0].level == "ERROR"


def test_textless_different_stream_does_not_merge() -> None:
    """没有正文、且流标识不同的消息会清掉待合并片段。"""
    sources = [
        _event(
            1,
            "agent.message",
            _at(0),
            sessionId="s1",
            turnId="t1",
            spanId="llm",
            content="first",
        ),
        _event(2, "agent.message", _at(1), sessionId="s2", turnId="t2", spanId="llm"),
        _event(
            3,
            "agent.message",
            _at(2),
            sessionId="s1",
            turnId="t1",
            spanId="llm",
            content="second",
        ),
    ]
    timeline = project_activities(54, sources)
    assert [item.content for item in timeline.activities] == ["first", "second"]


def test_malformed_detail_falls_back_to_stored_message() -> None:
    """detail 不是合法 JSON 时，活动改用已落库的 message。"""
    message = _event(1, "agent.message", _at(0), detail="{not valid json")
    message.message = "stored readable message"
    timeline = project_activities(55, [message])
    assert timeline.activities[0].content == "stored readable message"


def test_other_tenant_dispatch_is_missing() -> None:
    """时间线请求碰到其他工作空间的调度，按不存在处理。"""
    other = Dispatch(
        id=57,
        tenant_id=2,
        source_type="WORKITEM",
        workitem_id=1,
        agent_id=1,
        agent_version_id=1,
        status="RUNNING",
        attempt=1,
        idempotency_key="workitem-1",
        gmt_create=datetime(2026, 1, 2, 3, 4, 5),
        gmt_modified=datetime(2026, 1, 2, 3, 4, 5),
    )
    with pytest.raises(BizError) as caught:
        require_same_tenant(other, 1)
    assert caught.value.code == ErrorCode.DISPATCH_NOT_FOUND.code


def test_stored_error_on_agent_message_is_error_not_info() -> None:
    """带 error 列的消息按失败展示，不并进普通消息。"""
    source = _event(
        1,
        "agent.message",
        _at(0),
        sessionId="s1",
        turnId="t1",
        spanId="llm",
    )
    source.error = "stored provider error"
    timeline = project_activities(58, [source])
    assert len(timeline.activities) == 1
    assert timeline.activities[0].level == "ERROR"
    assert timeline.activities[0].content == "stored provider error"


def test_incomplete_stream_identifiers_stay_separate() -> None:
    """缺会话、回合或区间任一标识时，相邻消息不合并。"""
    sources = [
        _event(
            1,
            "agent.message",
            _at(0),
            turnId="t1",
            spanId="llm",
            content="missing-session-first",
        ),
        _event(
            2,
            "agent.message",
            _at(1),
            turnId="t1",
            spanId="llm",
            content="missing-session-second",
        ),
        _event(
            3,
            "agent.message",
            _at(2),
            sessionId="s2",
            spanId="llm",
            content="missing-turn-first",
        ),
        _event(
            4,
            "agent.message",
            _at(3),
            sessionId="s2",
            spanId="llm",
            content="missing-turn-second",
        ),
        _event(
            5,
            "agent.message",
            _at(4),
            sessionId="s3",
            turnId="t3",
            content="missing-span-first",
        ),
        _event(
            6,
            "agent.message",
            _at(5),
            sessionId="s3",
            turnId="t3",
            content="missing-span-second",
        ),
    ]
    timeline = project_activities(59, sources)
    assert [item.content for item in timeline.activities] == [
        "missing-session-first",
        "missing-session-second",
        "missing-turn-first",
        "missing-turn-second",
        "missing-span-first",
        "missing-span-second",
    ]


def test_empty_message_on_the_same_stream_keeps_pending() -> None:
    """同一流上没有正文的消息不打断，下一段继续拼接。"""
    sources = [
        _event(
            1,
            "agent.message",
            _at(0),
            sessionId="s1",
            turnId="t1",
            spanId="llm",
            content="first",
        ),
        _event(2, "agent.message", _at(1), sessionId="s1", turnId="t1", spanId="llm"),
        _event(
            3,
            "agent.message",
            _at(2),
            sessionId="s1",
            turnId="t1",
            spanId="llm",
            content="second",
        ),
    ]
    timeline = project_activities(60, sources)
    assert [item.content for item in timeline.activities] == ["firstsecond"]


def test_empty_message_with_incomplete_stream_clears_pending() -> None:
    """缺标识的空消息会清掉待合并片段。"""
    sources = [
        _event(
            1,
            "agent.message",
            _at(0),
            sessionId="s1",
            turnId="t1",
            spanId="llm",
            content="first",
        ),
        _event(2, "agent.message", _at(1), sessionId="s1", turnId="t1"),
        _event(
            3,
            "agent.message",
            _at(2),
            sessionId="s1",
            turnId="t1",
            spanId="llm",
            content="second",
        ),
    ]
    timeline = project_activities(61, sources)
    assert [item.content for item in timeline.activities] == ["first", "second"]


def test_failure_content_follows_priority_and_skips_textless() -> None:
    """失败正文按 error、reason、detail.error、message、detail.message 取值。"""
    stored_error = _event(
        1,
        "step.failed",
        _at(0),
        reason="detail reason",
        error="detail error",
        message="detail message",
    )
    stored_error.error = "stored error"
    stored_error.message = "stored message"
    detail_reason = _event(
        2,
        "step.failed",
        _at(1),
        reason="detail reason",
        error="detail error",
        message="detail message",
    )
    detail_reason.message = "stored message"
    detail_error = _event(
        3,
        "step.failed",
        _at(2),
        error="detail error",
        message="detail message",
    )
    detail_error.message = "stored message"
    stored_message = _event(4, "step.failed", _at(3), message="detail message")
    stored_message.message = "stored message"
    detail_message = _event(5, "step.failed", _at(4), message="detail message")
    textless = _event(6, "step.failed", _at(5))
    timeline = project_activities(
        62,
        [
            stored_error,
            detail_reason,
            detail_error,
            stored_message,
            detail_message,
            textless,
        ],
    )
    assert [item.content for item in timeline.activities] == [
        "stored error",
        "detail reason",
        "detail error",
        "stored message",
        "detail message",
    ]
    assert all(item.event_id != "45:6" for item in timeline.activities)


def test_outline_strips_prompts_while_turn_and_observation_keep_payloads() -> None:
    """大纲去掉提示词和观测载荷。按 id 读取时仍保留原文。"""
    payload = _TRACE_DOCUMENT.encode()
    full = trace_from_bytes(payload, 44)
    turn = find_turn(full, "turn-1")
    observation = require_observation(full, "call-1")
    assert turn.system_prompt == "full system prompt"
    assert observation.input["query"] == "posterior"
    assert observation.output == "result payload"
    outline = outline_from_bytes(payload, 44)
    outlined = outline.sessions[0].turns[0]
    child = outlined.observations[0].children[0]
    assert outline.source == "OSS"
    assert outline.dispatch_id == 44
    assert outlined.prompt is None
    assert outlined.system_prompt is None
    assert child.input is None
    assert child.output is None
    assert outlined.trace_id == "turn-1"
    assert outlined.context_files[0].content_ref == "context/files/abc"


def test_bad_dispatch_id_in_outline_falls_back_to_the_path_id() -> None:
    """大纲里的调度 id 无法解析时，改用路径上的 id。"""
    payload = b'{"dispatchId":"nope","sessions":[]}'
    outline = outline_from_bytes(payload, 91)
    assert outline.dispatch_id == 91
    with pytest.raises(BizError) as caught:
        trace_from_bytes(None, 91)
    assert caught.value.code == ErrorCode.ARTIFACT_NOT_FOUND.code


def test_context_ref_rejects_traversal() -> None:
    """上下文引用不能用相对路径穿越。"""
    validate_content_ref("context/files/abc")
    with pytest.raises(IllegalArgumentError, match="invalid context content ref"):
        validate_content_ref("../trace.json")


def test_artifact_name_matches_exact_or_suffix() -> None:
    """轨迹产物名可以是规范名，或以其结尾。"""
    assert name_matches("observability/trace.json", "observability/trace.json") is True
    suffix = "runs/observability/trace.json"
    assert name_matches(suffix, "observability/trace.json") is True
    assert name_matches("observability/other.json", "observability/trace.json") is False
    assert name_matches(None, "observability/trace.json") is False
    found = require_named_artifact([_artifact("bucket/trace")], "observability/trace.json")
    assert found.oss_ref == "bucket/trace"
    with pytest.raises(BizError) as caught:
        require_named_artifact([], "observability/trace.json")
    assert caught.value.code == ErrorCode.ARTIFACT_NOT_FOUND.code


def test_published_trace_prefers_outline() -> None:
    """有大纲时忽略事件投影，没有大纲时用投影。"""
    outline = RuntimeTrace(dispatch_id=1, source="OSS")
    projected = RuntimeTrace(dispatch_id=1, changed=False)
    assert choose_published_trace(outline, projected) is outline
    assert choose_published_trace(None, projected) is projected


def test_event_queries_follow_java_order() -> None:
    """轨迹按序号，活动按主键。两条语句都带租户和派发。"""
    seq_sql = _sql(events_by_seq_statement(1, 44))
    arrival_sql = _sql(events_by_arrival_statement(1, 44))
    artifact_sql = _sql(artifacts_for_dispatch(1, 44))
    assert "dispatch_runtime_event.tenant_id = 1" in seq_sql
    assert "dispatch_runtime_event.dispatch_id = 44" in seq_sql
    assert "coalesce(dispatch_runtime_event.seq, dispatch_runtime_event.id)" in seq_sql
    assert "dispatch_runtime_event.id ASC" in arrival_sql
    assert "coalesce" not in arrival_sql
    assert "artifact.tenant_id = 1" in artifact_sql
    assert "artifact.dispatch_id = 44" in artifact_sql
    assert "artifact.id DESC" in artifact_sql


def test_runtime_trace_and_live_activity_routes_require_login() -> None:
    """轨迹和实时活动都已注册。未登录时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "get" in paths["/api/dispatches/{id}/runtime-trace"]
    assert "get" in paths["/api/dispatches/{id}/runtime-trace/events"]
    assert "get" in paths["/api/dispatches/{id}/runtime-trace/activities"]
    assert "get" in paths["/api/dispatches/{id}/runtime-trace/turns/{traceId}"]
    observation = "/api/dispatches/{id}/runtime-trace/observations/{observationId}"
    assert "get" in paths[observation]
    assert "get" in paths["/api/dispatches/{id}/runtime-trace/context"]
    assert "get" in paths["/api/dispatches/{id}/live-activity"]
    response = client.get("/api/dispatches/44/runtime-trace")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    activities = client.get("/api/dispatches/44/runtime-trace/activities")
    assert activities.status_code == 401
    live = client.get("/api/dispatches/44/live-activity")
    assert live.status_code == 401
    assert live.json()["code"] == "10401"


def test_illegal_argument_returns_param_invalid_with_http_200() -> None:
    """非法参数沿用 Java advice：业务码 10001，HTTP 仍是 200。"""
    app = create_app()

    @app.get("/__illegal")
    def boom() -> None:
        raise IllegalArgumentError("invalid context content ref")

    client = TestClient(app)
    response = client.get("/__illegal")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "10001"
    assert body["message"] == "invalid context content ref"


def _at(second: int) -> datetime:
    start = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    return start + timedelta(seconds=second)


def _event(
    seq: int,
    event_type: str,
    event_time: datetime | None,
    detail: str | None = None,
    **fields: object,
) -> TraceSource:
    if detail is None:
        body = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    else:
        body = detail
    return TraceSource(
        event_id="45:" + str(seq),
        seq=seq,
        event_type=event_type,
        event_time=event_time,
        detail_json=body,
    )


def _tool_call() -> TraceSource:
    return _event(
        3,
        "bash.call",
        _at(2),
        sessionId="s1",
        turnId="t1",
        spanId="tool",
        input={"command": "cat /secrets", "token": "Bearer raw"},
    )


def _span(turn: RuntimeTurn, kind: str) -> RuntimeSpan:
    for span in turn.spans:
        if span.kind == kind:
            return span
    raise AssertionError(kind)


def _artifact(oss_ref: str) -> Artifact:
    return Artifact(
        id=1,
        tenant_id=1,
        workitem_id=1,
        dispatch_id=44,
        name="observability/trace.json",
        type="FILE",
        oss_ref=oss_ref,
        gmt_create=datetime(2026, 7, 30, 10, 0, 0),
    )


def _sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)
