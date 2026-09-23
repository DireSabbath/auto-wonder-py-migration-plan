"""把已落库的运行事件投影成轨迹和活动时间线。"""

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.clock import SHANGHAI
from autowonder.dispatch.models import Dispatch, DispatchRuntimeEvent
from autowonder.dispatch.query import require_same_tenant
from autowonder.dispatch.schemas import (
    RuntimeActivity,
    RuntimeActivityTimeline,
    RuntimeBoundary,
    RuntimeEvent,
    RuntimeSession,
    RuntimeSpan,
    RuntimeTrace,
    RuntimeTurn,
    TokenUsage,
)

_SESSION_KIND = {
    "session.started": "STARTED",
    "session.resumed": "RESUMED",
    "session.forked": "FORKED",
    "session.interrupted": "INTERRUPTED",
    "session.completed": "COMPLETED",
    "session.failed": "FAILED",
    "session.cancelled": "CANCELLED",
}
_TERMINAL_SESSION = frozenset({"INTERRUPTED", "COMPLETED", "FAILED", "CANCELLED"})
_OPENING_SESSION = frozenset({"STARTED", "RESUMED", "FORKED"})
_TURN_STATUS = {
    "turn.completed": "COMPLETED",
    "turn.failed": "FAILED",
    "turn.interrupted": "INTERRUPTED",
}
_SUCCESS_STATUS = frozenset({"ok", "success", "succeeded", "completed", "0"})
_FAILURE_TYPES = frozenset({"step.failed", "session.failed", "dispatch.failed"})


@dataclass
class TraceSource:
    """一条已落库的运行事件。投影函数不访问数据库。"""

    event_id: str | None = None
    seq: int | None = None
    event_type: str | None = None
    event_time: datetime | None = None
    gmt_create: datetime | None = None
    detail_json: Any = None
    step_id: int | None = None
    step_key: str | None = None
    step_name: str | None = None
    agent_id: int | None = None
    message: str | None = None
    error: str | None = None


def project_trace(
    dispatch_id: int,
    sources: list[TraceSource],
    after_seq: int | None,
) -> RuntimeTrace:
    """按序号折叠会话、回合、区间和用量。序号未前进时只返回末序号。"""
    last_seq = _last_seq(sources)
    trace = RuntimeTrace(dispatch_id=dispatch_id, last_seq=last_seq)
    if after_seq is not None and last_seq <= after_seq:
        trace.changed = False
        return trace

    sessions: dict[str, RuntimeSession] = {}
    turns: dict[str, RuntimeTurn] = {}
    spans: dict[str, RuntimeSpan] = {}
    session_first: dict[str, datetime] = {}
    session_last: dict[str, datetime] = {}
    current_step_id: str | None = None
    current_step_name: str | None = None
    for source in sources:
        event = _to_event(source)
        trace.events.append(event)
        event_type = _event_type(source)
        if event_type == "step.started":
            current_step_id = _first(_text(event.detail, "stepId"), source.step_key)
            current_step_name = _first(_text(event.detail, "stepName"), source.step_name)
        if trace.runtime_id is None:
            trace.runtime_id = _text(event.detail, "runtimeId")
        if trace.provider is None:
            trace.provider = _text(event.detail, "provider")
        session_id = _text(event.detail, "sessionId")
        if session_id is None:
            continue
        _remember_session_time(session_first, session_last, session_id, source.event_time)
        session = _session(trace, sessions, session_id, event.detail)
        if source.event_id is not None:
            session.event_ids.append(source.event_id)
        _apply_session(session, event_type, event)
        turn_id = _text(event.detail, "turnId")
        if turn_id is None:
            continue
        turn = _turn(session, turns, session_id, turn_id, current_step_id, current_step_name)
        if source.event_id is not None:
            turn.event_ids.append(source.event_id)
        _apply_turn(turn, event_type, event)
        kind = _span_kind(event_type, event.detail)
        span_id = _text(event.detail, "spanId")
        if kind is None or span_id is None:
            continue
        span = _span(turn, spans, session_id, turn_id, kind, span_id, event.detail)
        if source.event_id is not None:
            span.event_ids.append(source.event_id)
        _apply_span(span, event_type, event)
        if event_type == "llm.usage":
            _add_usage_detail(span.token_usage, event.detail)
            _add_usage_detail(turn.token_usage, event.detail)
    _roll_up(trace, session_first, session_last)
    return trace


def project_activities(dispatch_id: int, sources: list[TraceSource]) -> RuntimeActivityTimeline:
    """按到达顺序合并同一流的消息，并丢掉会泄漏的结构化文本。"""
    timeline = RuntimeActivityTimeline(dispatch_id=dispatch_id)
    pending: RuntimeActivity | None = None
    pending_session: str | None = None
    pending_turn: str | None = None
    pending_span: str | None = None
    for source in sources:
        event_type = _event_type(source)
        detail = _detail_of(source.detail_json)
        if _has_error(source, event_type):
            pending = None
            content = _error_content(source, detail)
            if content is not None:
                timeline.activities.append(_activity(source, "ERROR", content))
            continue
        if event_type == "agent.message":
            pending, pending_session, pending_turn, pending_span = _fold_message(
                timeline,
                source,
                detail,
                pending,
                pending_session,
                pending_turn,
                pending_span,
            )
            continue
        pending = None
    return timeline


def events_by_seq_statement(
    tenant_id: int,
    dispatch_id: int,
) -> Select[tuple[DispatchRuntimeEvent]]:
    """同一派发的事件按序号，空序号退回主键。"""
    return (
        select(DispatchRuntimeEvent)
        .where(
            DispatchRuntimeEvent.tenant_id == tenant_id,
            DispatchRuntimeEvent.dispatch_id == dispatch_id,
        )
        .order_by(
            func.coalesce(DispatchRuntimeEvent.seq, DispatchRuntimeEvent.id).asc(),
            DispatchRuntimeEvent.id.asc(),
        )
    )


def events_by_arrival_statement(
    tenant_id: int,
    dispatch_id: int,
) -> Select[tuple[DispatchRuntimeEvent]]:
    """活动时间线按插入顺序，不看序号。"""
    return (
        select(DispatchRuntimeEvent)
        .where(
            DispatchRuntimeEvent.tenant_id == tenant_id,
            DispatchRuntimeEvent.dispatch_id == dispatch_id,
        )
        .order_by(DispatchRuntimeEvent.id.asc())
    )


def source_from_row(row: DispatchRuntimeEvent) -> TraceSource:
    """登记行收成投影输入。"""
    return TraceSource(
        event_id=row.event_id,
        seq=row.seq,
        event_type=row.event_type,
        event_time=row.event_time,
        gmt_create=row.gmt_create,
        detail_json=row.detail_json,
        step_id=row.step_id,
        step_key=row.step_key,
        step_name=row.step_name,
        agent_id=row.agent_id,
        message=row.message,
        error=row.error,
    )


async def load_projected_trace(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    after_seq: int | None,
) -> RuntimeTrace:
    """读取持久化事件并投影。其他工作空间视为调度不存在。"""
    await _require_dispatch(session, tenant_id, dispatch_id)
    rows = await session.scalars(events_by_seq_statement(tenant_id, dispatch_id))
    sources = [source_from_row(row) for row in rows]
    return project_trace(dispatch_id, sources, after_seq)


async def load_activities(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> RuntimeActivityTimeline:
    """按到达顺序投影活动时间线。"""
    await _require_dispatch(session, tenant_id, dispatch_id)
    rows = await session.scalars(events_by_arrival_statement(tenant_id, dispatch_id))
    sources = [source_from_row(row) for row in rows]
    return project_activities(dispatch_id, sources)


def choose_published_trace(
    outline: RuntimeTrace | None,
    projected: RuntimeTrace,
) -> RuntimeTrace:
    """有完成态大纲时返回大纲，否则返回事件投影。"""
    if outline is None:
        return projected
    return outline


async def _require_dispatch(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> Dispatch:
    row = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    return require_same_tenant(row, tenant_id)


def _roll_up(
    trace: RuntimeTrace,
    session_first: dict[str, datetime],
    session_last: dict[str, datetime],
) -> None:
    for session in trace.sessions:
        for turn in session.turns:
            _add_usage_total(session.token_usage, turn.token_usage)
        session_key = session.session_id
        if session_key is not None:
            _set_session_duration(
                session,
                session_first.get(session_key),
                session_last.get(session_key),
            )
        _add_usage_total(trace.token_usage, session.token_usage)


def _set_session_duration(
    session: RuntimeSession,
    first_event: datetime | None,
    last_event: datetime | None,
) -> None:
    if first_event is None or last_event is None:
        return
    millis = (last_event - first_event) // timedelta(milliseconds=1)
    if millis < 0:
        millis = 0
    session.duration_ms = millis


def _remember_session_time(
    session_first: dict[str, datetime],
    session_last: dict[str, datetime],
    session_id: str,
    event_time: datetime | None,
) -> None:
    if event_time is None:
        return
    moment = _as_utc(event_time)
    current_first = session_first.get(session_id)
    if current_first is None or moment < current_first:
        session_first[session_id] = moment
    current_last = session_last.get(session_id)
    if current_last is None or moment > current_last:
        session_last[session_id] = moment


def _session(
    trace: RuntimeTrace,
    sessions: dict[str, RuntimeSession],
    session_id: str,
    detail: dict[str, Any],
) -> RuntimeSession:
    found = sessions.get(session_id)
    if found is not None:
        return found
    parent = _text(detail, "parentSessionId")
    if parent == session_id:
        parent = None
    created = RuntimeSession(session_id=session_id, parent_session_id=parent, status="RUNNING")
    sessions[session_id] = created
    trace.sessions.append(created)
    return created


def _turn(
    session: RuntimeSession,
    turns: dict[str, RuntimeTurn],
    session_id: str,
    turn_id: str,
    step_id: str | None,
    step_name: str | None,
) -> RuntimeTurn:
    key = session_id + "\u0000" + turn_id
    found = turns.get(key)
    if found is not None:
        return found
    created = RuntimeTurn(
        turn_id=turn_id,
        step_id=step_id,
        step_name=step_name,
        status="RUNNING",
    )
    turns[key] = created
    session.turns.append(created)
    return created


def _span(
    turn: RuntimeTurn,
    spans: dict[str, RuntimeSpan],
    session_id: str,
    turn_id: str,
    kind: str,
    span_id: str,
    detail: dict[str, Any],
) -> RuntimeSpan:
    key = session_id + "\u0000" + turn_id + "\u0000" + kind + "\u0000" + span_id
    found = spans.get(key)
    if found is not None:
        return found
    created = RuntimeSpan(
        span_id=span_id,
        parent_span_id=_text(detail, "parentSpanId"),
        kind=kind,
        status="RUNNING",
    )
    spans[key] = created
    turn.spans.append(created)
    return created


def _apply_session(session: RuntimeSession, event_type: str, event: RuntimeEvent) -> None:
    kind = _SESSION_KIND.get(event_type)
    if kind is None:
        return
    session.boundaries.append(
        RuntimeBoundary(
            event_id=event.event_id,
            kind=kind,
            event_time=event.event_time,
            label=_boundary_label(kind, event.detail),
        )
    )
    if session.started_at is None and kind in _OPENING_SESSION:
        session.started_at = event.event_time
    if kind in _TERMINAL_SESSION:
        session.status = kind
        session.ended_at = event.event_time
        return
    session.status = "RUNNING"


def _boundary_label(kind: str, detail: dict[str, Any]) -> str:
    reason = _text(detail, "reason")
    checkpoint = _text(detail, "checkpointSeq")
    if reason is not None and checkpoint is not None:
        return kind + " · " + reason + " · checkpoint #" + checkpoint
    if reason is None:
        return kind
    return kind + " · " + reason


def _apply_turn(turn: RuntimeTurn, event_type: str, event: RuntimeEvent) -> None:
    if event_type == "turn.started":
        turn.started_at = event.event_time
        turn.status = "RUNNING"
        turn.prompt = _first(_text(event.detail, "prompt"), turn.prompt)
        turn.system_prompt = _first(_text(event.detail, "systemPrompt"), turn.system_prompt)
        return
    status = _TURN_STATUS.get(event_type)
    if status is None:
        return
    turn.status = status
    turn.ended_at = event.event_time
    turn.duration_ms = _duration(event.detail, turn.started_at, event.event_time)


def _apply_span(span: RuntimeSpan, event_type: str, event: RuntimeEvent) -> None:
    detail = event.detail
    next_name = _first(
        _skill_name(detail),
        _text(detail, "tool"),
        _text(detail, "model"),
        _text(detail, "name"),
    )
    if span.kind == "SKILL" and _equals_ignore_case(next_name, "skill") and span.name is not None:
        next_name = span.name
    span.name = _first(next_name, span.name, span.kind)
    span.model = _first(_text(detail, "model"), span.model)
    span.input_summary = _first(_text(detail, "inputSummary"), span.input_summary)
    span.output_summary = _first(
        _text(detail, "outputSummary"),
        _text(detail, "contentSummary"),
        span.output_summary,
    )
    if "input" in detail:
        span.input = detail["input"]
    span.output = _first(_text(detail, "output"), span.output)
    content = _text(detail, "content")
    if content is not None and event_type == "agent.message":
        if span.content is None:
            span.content = content
        else:
            span.content = span.content + content
    else:
        span.content = _first(content, span.content)
    span.error_category = _first(_text(detail, "errorCategory"), span.error_category)
    if _is_span_start(event_type):
        if span.started_at is None:
            span.started_at = event.event_time
        if _is_instant_span(event_type):
            span.status = "COMPLETED"
        else:
            span.status = "RUNNING"
    if _is_span_end(event_type):
        span.ended_at = event.event_time
        span.duration_ms = _duration(detail, span.started_at, event.event_time)
        if _is_successful(detail):
            span.status = "COMPLETED"
        else:
            span.status = "FAILED"


def _span_kind(event_type: str, detail: dict[str, Any]) -> str | None:
    if event_type.startswith("llm.thinking_"):
        return "THINKING"
    # Qoder 的 llm.* 是 provider 回合外壳，不是精确的模型请求边界。
    if event_type.startswith("llm."):
        return "PROVIDER"
    if event_type.startswith("bash."):
        return "BASH"
    if event_type.startswith("cli."):
        return "CLI"
    if event_type.startswith("mcp."):
        return "MCP"
    tool_name = _text(detail, "tool")
    if event_type.startswith("agent.tool_") and _equals_ignore_case(tool_name, "skill"):
        return "SKILL"
    if event_type.startswith("agent.tool_"):
        return "TOOL"
    if event_type == "agent.message" and detail.get("providerEvent") is True:
        return "PROVIDER"
    if event_type.startswith("skill."):
        return "SKILL"
    if event_type.startswith("guidance."):
        return "GUIDANCE"
    if event_type.startswith("artifact."):
        return "ARTIFACT"
    return None


def _skill_name(detail: dict[str, Any]) -> str | None:
    raw_input = detail.get("input")
    if not isinstance(raw_input, dict):
        return None
    value = raw_input.get("skill")
    if value is None:
        return None
    return _java_string(value)


def _is_span_start(event_type: str) -> bool:
    if event_type.endswith(".started"):
        return True
    if event_type.endswith(".call"):
        return True
    if event_type.endswith(".tool_use"):
        return True
    if event_type.endswith(".loaded"):
        return True
    if event_type.endswith(".invoked"):
        return True
    if event_type.endswith(".received"):
        return True
    return False


def _is_instant_span(event_type: str) -> bool:
    if event_type.endswith(".loaded"):
        return True
    if event_type.endswith(".invoked"):
        return True
    if event_type.endswith(".received"):
        return True
    if event_type.startswith("artifact."):
        return True
    return False


def _is_span_end(event_type: str) -> bool:
    if event_type.endswith(".completed"):
        return True
    if event_type.endswith(".failed"):
        return True
    if event_type.endswith(".result"):
        return True
    if event_type.endswith(".tool_result"):
        return True
    if event_type.endswith(".applied"):
        return True
    return False


def _is_successful(detail: dict[str, Any]) -> bool:
    value = _text(detail, "status")
    if value is None:
        return _text(detail, "errorCategory") is None
    return value.lower() in _SUCCESS_STATUS


def _duration(detail: dict[str, Any], started_at: str | None, ended_at: str | None) -> int | None:
    explicit = _long_value(detail.get("durationMs"))
    if explicit is not None:
        return explicit
    if started_at is None or ended_at is None:
        return None
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    millis = (end - start) // timedelta(milliseconds=1)
    if millis < 0:
        return 0
    return millis


def _add_usage_detail(target: TokenUsage, detail: dict[str, Any]) -> None:
    target.available = True
    target.input_tokens = target.input_tokens + _usage_value(detail, "inputTokens")
    target.output_tokens = target.output_tokens + _usage_value(detail, "outputTokens")
    target.reasoning_tokens = target.reasoning_tokens + _usage_value(detail, "reasoningTokens")
    target.cache_read_tokens = target.cache_read_tokens + _usage_value(detail, "cacheReadTokens")
    written = _usage_value(detail, "cacheWriteTokens")
    target.cache_write_tokens = target.cache_write_tokens + written
    target.total_tokens = target.input_tokens + target.output_tokens


def _add_usage_total(target: TokenUsage, source: TokenUsage) -> None:
    if not source.available:
        return
    target.available = True
    target.input_tokens = target.input_tokens + source.input_tokens
    target.output_tokens = target.output_tokens + source.output_tokens
    target.reasoning_tokens = target.reasoning_tokens + source.reasoning_tokens
    target.cache_read_tokens = target.cache_read_tokens + source.cache_read_tokens
    target.cache_write_tokens = target.cache_write_tokens + source.cache_write_tokens
    target.total_tokens = target.input_tokens + target.output_tokens


def _usage_value(detail: dict[str, Any], key: str) -> int:
    value = _long_value(detail.get(key))
    if value is None:
        return 0
    return value


def _long_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if value is None:
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _fold_message(
    timeline: RuntimeActivityTimeline,
    source: TraceSource,
    detail: dict[str, Any],
    pending: RuntimeActivity | None,
    pending_session: str | None,
    pending_turn: str | None,
    pending_span: str | None,
) -> tuple[RuntimeActivity | None, str | None, str | None, str | None]:
    session_id = _textual(detail, "sessionId")
    turn_id = _textual(detail, "turnId")
    span_id = _textual(detail, "spanId")
    complete = session_id is not None and turn_id is not None and span_id is not None
    matches = False
    if pending is not None and pending_session == session_id:
        if pending_turn == turn_id and pending_span == span_id:
            matches = True
    content = _first(_activity_text(detail.get("content")), _activity_text(source.message))
    if content is None:
        if complete and matches:
            return pending, pending_session, pending_turn, pending_span
        return None, pending_session, pending_turn, pending_span
    if complete and matches and pending is not None:
        pending.content = cast(str, pending.content) + content
        return pending, pending_session, pending_turn, pending_span
    created = _activity(source, "INFO", content)
    timeline.activities.append(created)
    return created, session_id, turn_id, span_id


def _activity(source: TraceSource, level: str, content: str) -> RuntimeActivity:
    return RuntimeActivity(
        event_id=source.event_id,
        seq=source.seq,
        event_time=_activity_time(source),
        event_type=source.event_type,
        level=level,
        content=content,
    )


def _has_error(source: TraceSource, event_type: str) -> bool:
    if _activity_text(source.error) is not None:
        return True
    return event_type in _FAILURE_TYPES


def _error_content(source: TraceSource, detail: dict[str, Any]) -> str | None:
    return _first(
        _activity_text(source.error),
        _activity_text(detail.get("reason")),
        _activity_text(detail.get("error")),
        _activity_text(source.message),
        _activity_text(detail.get("message")),
    )


def _to_event(source: TraceSource) -> RuntimeEvent:
    event_time = None
    if source.event_time is not None:
        event_time = instant_text(source.event_time)
    return RuntimeEvent(
        event_id=source.event_id,
        seq=source.seq,
        event_type=source.event_type,
        event_time=event_time,
        detail=_detail_of(source.detail_json),
    )


def _activity_time(source: TraceSource) -> str | None:
    if source.event_time is not None:
        return instant_text(source.event_time)
    if source.gmt_create is None:
        return None
    return instant_text(source.gmt_create)


def _detail_of(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _event_type(source: TraceSource) -> str:
    if source.event_type is None:
        return ""
    return source.event_type


def _last_seq(sources: list[TraceSource]) -> int:
    last = 0
    for source in sources:
        if source.seq is None:
            continue
        if source.seq > last:
            last = source.seq
    return last


def _textual(detail: dict[str, Any], key: str) -> str | None:
    value = detail.get(key)
    if not isinstance(value, str):
        return None
    if _is_blank(value):
        return None
    return value


def _activity_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if _is_blank(value):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, dict) or isinstance(parsed, list):
        return None
    return value


def _text(detail: dict[str, Any], key: str) -> str | None:
    if key not in detail:
        return None
    value = detail[key]
    if value is None:
        return None
    rendered = _java_string(value)
    if _is_blank(rendered):
        return None
    return rendered


def _first(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        if _is_blank(value):
            continue
        return value
    return None


def _equals_ignore_case(value: str | None, expected: str) -> bool:
    if value is None:
        return False
    return value.lower() == expected.lower()


def _java_string(value: object) -> str:
    if isinstance(value, bool):
        if value:
            return "true"
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, dict) or isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def instant_text(moment: datetime) -> str:
    """写成 Java ``Instant.toString`` 的 UTC 文本。"""
    if moment.tzinfo is None:
        aware = moment.replace(tzinfo=SHANGHAI)
    else:
        aware = moment
    utc = aware.astimezone(ZoneInfo("UTC"))
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond != 0:
        fraction = f"{utc.microsecond:06d}".rstrip("0")
        text = text + "." + fraction
    return text + "Z"


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=SHANGHAI).astimezone(ZoneInfo("UTC"))
    return moment.astimezone(ZoneInfo("UTC"))


def _is_blank(text: str) -> bool:
    for char in text:
        if not _is_java_whitespace(ord(char)):
            return False
    return True


def _is_java_whitespace(code_point: int) -> bool:
    if code_point in {0x00A0, 0x2007, 0x202F}:
        return False
    if code_point in {0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F}:
        return True
    category = unicodedata.category(chr(code_point))
    if category == "Zs" or category == "Zl" or category == "Zp":
        return True
    return False
