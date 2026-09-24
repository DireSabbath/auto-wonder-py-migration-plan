"""把运行事件投影成浏览器可见的实时活动。只放行允许的类型和摘要字段。"""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.dispatch.action_text import (
    java_is_blank,
    looks_like_mojibake,
    looks_sensitive,
    sanitize,
)
from autowonder.dispatch.models import Dispatch, DispatchRuntimeEvent
from autowonder.dispatch.query import execution_source_type, require_same_tenant
from autowonder.dispatch.schemas import LiveAction, LiveActivity
from autowonder.dispatch.trace import (
    TraceSource,
    events_by_seq_statement,
    instant_text,
    source_from_row,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_SUMMARY_KEYS = (
    "message",
    "summary",
    "text",
    "resultSummary",
    "inputSummary",
    "outputSummary",
    "reason",
)
_TARGET_KEYS = ("tool", "name", "stepName")
_ALLOWED_PREFIXES = (
    "package.",
    "bootstrap.",
    "workspace.",
    "repo.",
    "step.",
    "sdlc.",
    "dispatch.",
    "artifact.",
    "upload.",
    "agent.",
    "bash.",
    "cli.",
    "mcp.",
    "skill.",
    "plugin.",
    "session.",
    "turn.",
    "task.",
    "subagent.",
    "handoff.",
)
_ALLOWED_EXACT = frozenset({"completion_requested"})
_DENIED_EXACT = frozenset({"agent.message"})
_DENIED_PREFIXES = ("llm.", "guidance.", "comment.")
_DENIED_SUBSTRINGS = ("thinking", "reasoning", "chain_of_thought", "chainofthought")
_SEARCH_READ = frozenset({"read", "grep", "glob", "ls", "search", "webfetch", "websearch", "view"})
_FILE_EDIT = frozenset(
    {"edit", "write", "multiedit", "notebookedit", "apply_patch", "create_file"}
)
_COMMANDS = frozenset({"bash", "shell", "run", "execute", "runcommand"})
_SKILLS = frozenset({"skill", "plugin"})
_SUBAGENTS = frozenset({"agent", "task", "subagent", "dispatch_agent"})
_FAILED_STATUS = frozenset({"failed", "error", "failure", "timeout"})
_CANCELLED_STATUS = frozenset({"cancelled", "canceled", "aborted"})
_PAUSED_STATUS = frozenset({"paused", "pausing", "interrupted"})


@dataclass
class LiveDispatch:
    """投影实时活动所需的调度字段。"""

    id: int
    agent_id: int
    workitem_id: int
    source_type: str | None
    attempt: int
    status: str | None


class LiveActivityMetrics:
    """记录回补、过滤和截断次数。标签收成固定桶，避免事件名膨胀。"""

    def __init__(self) -> None:
        self.backfills = 0
        self.filtered: list[str] = []
        self.truncated_marks: list[int] = []

    def backfill(self) -> None:
        """带 afterSeq 的读取算一次回补。"""
        self.backfills += 1

    def filtered_read(self, event_type: str | None) -> None:
        """投影时丢弃的事件按桶计数。"""
        self.filtered.append(event_bucket(event_type))

    def truncated(self, dropped: int) -> None:
        """窗口丢掉的旧动作按条数记一次。"""
        if dropped > 0:
            self.truncated_marks.append(dropped)


def event_bucket(event_type: str | None) -> str:
    """把任意事件类型收成固定标签。"""
    if event_type is None or java_is_blank(event_type):
        return "none"
    lowered = event_type.lower()
    if lowered.startswith("llm."):
        return "llm"
    if lowered.startswith("guidance."):
        return "guidance"
    if lowered.startswith("comment."):
        return "comment"
    if lowered.startswith("agent.message"):
        return "agent_message"
    if "thinking" in lowered or "reasoning" in lowered or "chain_of_thought" in lowered:
        return "reasoning"
    return "other"


def project_live(
    dispatch: LiveDispatch,
    sources: list[TraceSource],
    after_seq: int | None,
    limit: int | None,
    metrics: LiveActivityMetrics | None = None,
) -> LiveActivity:
    """按允许名单生成动作。序号未前进时只返回末序号。"""
    recorder = metrics
    if recorder is None:
        recorder = LiveActivityMetrics()
    if after_seq is not None:
        recorder.backfill()
        last_seq = _max_seq(sources, after_seq)
    else:
        last_seq = _max_seq(sources, 0)
    activity = LiveActivity(
        dispatch_id=dispatch.id,
        agent_id=dispatch.agent_id,
        workitem_id=dispatch.workitem_id,
        source_type=execution_source_type(dispatch.source_type),
        attempt=dispatch.attempt,
        dispatch_status=dispatch.status,
        last_seq=last_seq,
    )
    if after_seq is not None and last_seq <= after_seq:
        activity.changed = False
        return activity

    displayable: list[LiveAction] = []
    for source in sources:
        if after_seq is not None and source.seq is not None and source.seq <= after_seq:
            continue
        action = to_action(dispatch, source)
        if action is None:
            recorder.filtered_read(source.event_type)
            continue
        displayable.append(action)
    activity.total_actions = len(displayable)
    if after_seq is None and len(displayable) == 0:
        activity.awaiting_runtime = True
    window = normalize_limit(limit)
    dropped = len(displayable) - window
    if dropped < 0:
        dropped = 0
    recorder.truncated(dropped)
    activity.truncated = dropped > 0
    newest = list(displayable[dropped:])
    newest.reverse()
    activity.actions = newest
    if len(displayable) > 0:
        activity.last_updated_at = displayable[-1].event_time
        index = len(displayable) - 1
        while index >= 0:
            if displayable[index].status == "RUNNING":
                activity.current_action = displayable[index]
                break
            index -= 1
    return activity


def to_action(dispatch: LiveDispatch, event: TraceSource | None) -> LiveAction | None:
    """一条事件收成可见动作。不允许展示时返回空。"""
    if event is None:
        return None
    detail = _parse_detail(event.detail_json)
    kind = action_type(event.event_type, detail)
    if kind is None:
        return None
    agent_id = event.agent_id
    if agent_id is None:
        agent_id = dispatch.agent_id
    event_time = None
    if event.event_time is not None:
        event_time = instant_text(event.event_time)
    return LiveAction(
        event_id=event.event_id,
        seq=event.seq,
        event_time=event_time,
        event_type=event.event_type,
        action_type=kind,
        status=action_status(event.event_type, detail, event.error),
        step_id=event.step_id,
        step_key=event.step_key,
        step_name=sanitize(event.step_name),
        agent_id=agent_id,
        dispatch_id=dispatch.id,
        attempt=dispatch.attempt,
        summary=_summary(event, detail, kind, event.event_type),
    )


def action_type(event_type: str | None, detail: dict[str, Any]) -> str | None:
    """事件类型收成稳定的展示类别。思维链和模型原文没有类别。"""
    if not is_displayable(event_type):
        return None
    lowered = ""
    if event_type is not None:
        lowered = event_type.lower()
    if lowered.startswith("package.") or lowered.startswith("bootstrap."):
        return "CONTEXT_PREPARE"
    if lowered.startswith("workspace."):
        return "CONTEXT_PREPARE"
    if lowered.startswith("repo."):
        return "REPO_PREPARE"
    if lowered.startswith("step.") or lowered.startswith("sdlc."):
        return "SDLC_STEP"
    if lowered == "completion_requested":
        return "SDLC_STEP"
    if lowered.startswith("dispatch."):
        return "DISPATCH"
    if lowered.startswith("handoff."):
        return "HANDOFF"
    if lowered.startswith("artifact.") or lowered.startswith("upload."):
        return "ARTIFACT"
    if lowered.startswith("bash.") or lowered.startswith("cli."):
        return "COMMAND"
    if lowered == "skill.loaded":
        return "SKILL_LOAD"
    if lowered == "plugin.loaded":
        return "PLUGIN_LOAD"
    if lowered == "mcp.loaded":
        return "MCP_LOAD"
    if lowered.startswith("mcp."):
        return "MCP_CALL"
    if lowered.startswith("skill.") or lowered.startswith("plugin."):
        return "SKILL"
    if lowered.startswith("session."):
        return "SESSION"
    if lowered.startswith("turn."):
        return "MODEL_TURN"
    if lowered.startswith("task.") or lowered.startswith("subagent."):
        return "SUBAGENT"
    if lowered.startswith("agent.tool_use") or lowered.startswith("agent.tool_result"):
        return tool_action_type(_text(detail, "tool"))
    return "AGENT"


def tool_action_type(tool: str | None) -> str:
    """工具名收成检索、编辑、命令、技能或子代理。"""
    if tool is None or java_is_blank(tool):
        return "TOOL"
    normalized = _java_trim(tool).lower()
    if normalized.startswith("mcp__") or normalized.startswith("mcp."):
        return "MCP_CALL"
    if normalized in _SEARCH_READ:
        return "SEARCH_READ"
    if normalized in _FILE_EDIT:
        return "FILE_EDIT"
    if normalized in _COMMANDS:
        return "COMMAND"
    if normalized in _SKILLS:
        return "SKILL"
    if normalized in _SUBAGENTS:
        return "SUBAGENT"
    return "TOOL"


def is_displayable(event_type: str | None) -> bool:
    """空白、模型原文、指导和思维链标记都不进实时活动。"""
    if event_type is None or java_is_blank(event_type):
        return False
    lowered = event_type.lower()
    if lowered in _DENIED_EXACT:
        return False
    for denied in _DENIED_PREFIXES:
        if lowered.startswith(denied):
            return False
    for denied in _DENIED_SUBSTRINGS:
        if denied in lowered:
            return False
    if lowered in _ALLOWED_EXACT:
        return True
    for allowed in _ALLOWED_PREFIXES:
        if lowered.startswith(allowed):
            return True
    return False


def action_status(event_type: str | None, detail: dict[str, Any], error: str | None) -> str:
    """错误列优先。其余按上报状态和事件后缀归类。"""
    if error is not None and not java_is_blank(error):
        return "FAILED"
    lowered = ""
    if event_type is not None:
        lowered = event_type.lower()
    reported = _text(detail, "status")
    if reported is not None:
        normalized = reported.lower()
        if normalized in _FAILED_STATUS:
            return "FAILED"
        if normalized in _CANCELLED_STATUS:
            return "CANCELLED"
        if normalized in _PAUSED_STATUS:
            return "PAUSED"
    if lowered.endswith(".failed") or lowered.endswith(".error") or lowered.endswith(".timeout"):
        return "FAILED"
    if lowered.endswith(".cancelled") or lowered.endswith(".canceled"):
        return "CANCELLED"
    if (
        lowered == "session.interrupted"
        or lowered.endswith(".paused")
        or lowered.endswith(".pausing")
    ):
        return "PAUSED"
    if lowered == "session.resumed" or lowered.endswith(".resumed") or lowered.endswith(".resume"):
        return "RESUMED"
    if _is_running_type(lowered):
        return "RUNNING"
    if _is_completed_type(lowered):
        return "COMPLETED"
    return "INFO"


def label(kind: str, event_type: str | None) -> str:
    """类别和事件类型对应的中文短句。"""
    lowered = ""
    if event_type is not None:
        lowered = event_type.lower()
    if kind == "CONTEXT_PREPARE":
        return "准备任务包与上下文"
    if kind == "REPO_PREPARE":
        return "准备工作仓库"
    if kind == "SDLC_STEP":
        return _step_label(lowered)
    if kind == "DISPATCH":
        return _dispatch_label(lowered)
    if kind == "HANDOFF":
        return _handoff_label(lowered)
    if kind == "ARTIFACT":
        return "生成并上传产物"
    if kind == "COMMAND":
        return "执行命令"
    if kind == "SKILL_LOAD":
        return "已加载 Skill"
    if kind == "PLUGIN_LOAD":
        return "已加载 Plugin"
    if kind == "MCP_LOAD":
        return "已加载 MCP 服务"
    if kind == "MCP_CALL":
        return "调用 MCP 工具"
    if kind == "SKILL":
        return "调用 Skill"
    if kind == "SESSION":
        return _session_label(lowered)
    if kind == "MODEL_TURN":
        return _turn_label(lowered)
    if kind == "SUBAGENT":
        return "调度子代理"
    if kind == "SEARCH_READ":
        return "检索或读取代码"
    if kind == "FILE_EDIT":
        return "修改文件"
    if kind == "TOOL":
        return "调用工具"
    return "Agent 进度更新"


def normalize_limit(limit: int | None) -> int:
    """缺省和小于等于 0 用 50，上限 200。"""
    if limit is None or limit <= 0:
        return DEFAULT_LIMIT
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def events_after_seq_statement(
    tenant_id: int,
    dispatch_id: int,
    after_seq: int,
) -> Select[tuple[DispatchRuntimeEvent]]:
    """序号大于游标，或序号为空的事件。仍按序号排序。"""
    return (
        select(DispatchRuntimeEvent)
        .where(
            DispatchRuntimeEvent.tenant_id == tenant_id,
            DispatchRuntimeEvent.dispatch_id == dispatch_id,
            or_(
                DispatchRuntimeEvent.seq.is_(None),
                DispatchRuntimeEvent.seq > after_seq,
            ),
        )
        .order_by(
            func.coalesce(DispatchRuntimeEvent.seq, DispatchRuntimeEvent.id).asc(),
            DispatchRuntimeEvent.id.asc(),
        )
    )


async def load_live_activity(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    after_seq: int | None,
    limit: int | None,
) -> LiveActivity:
    """读取一条调度的实时活动。其他工作空间视为调度不存在。"""
    row = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    found = require_same_tenant(row, tenant_id)
    if after_seq is None:
        statement = events_by_seq_statement(tenant_id, dispatch_id)
    else:
        statement = events_after_seq_statement(tenant_id, dispatch_id, after_seq)
    rows = await session.scalars(statement)
    sources = [source_from_row(item) for item in rows]
    dispatch = LiveDispatch(
        id=found.id,
        agent_id=found.agent_id,
        workitem_id=found.workitem_id,
        source_type=found.source_type,
        attempt=found.attempt,
        status=found.status,
    )
    return project_live(dispatch, sources, after_seq, limit)


def _summary(
    event: TraceSource,
    detail: dict[str, Any],
    kind: str,
    event_type: str | None,
) -> str:
    reported = _first_text(detail, _SUMMARY_KEYS)
    if reported is None and event.message is not None and not java_is_blank(event.message):
        if not looks_like_mojibake(event.message):
            reported = event.message
    if event.error is not None and not java_is_blank(event.error):
        if not looks_like_mojibake(event.error):
            if reported is None:
                reported = event.error
            else:
                reported = reported + " · " + event.error
    cleaned = sanitize(reported)
    if cleaned is not None and not looks_sensitive(cleaned):
        return cleaned
    target = sanitize(_first_text(detail, _TARGET_KEYS), 48)
    caption = label(kind, event_type)
    if target is None:
        return caption
    return caption + " · " + target


def _step_label(lowered: str) -> str:
    if lowered == "step.gate_started":
        return "开始校验"
    if lowered == "step.gate_finished":
        return "校验完成"
    if lowered == "step.fix_required":
        return "需要修复"
    if lowered == "step.failed":
        return "步骤执行失败"
    if lowered == "completion_requested":
        return "请求完成校验"
    if lowered.endswith(".started"):
        return "开始 SDLC 步骤"
    return "SDLC 步骤更新"


def _dispatch_label(lowered: str) -> str:
    if lowered == "dispatch.started":
        return "开始执行"
    if lowered == "dispatch.completed":
        return "执行完成"
    if lowered == "dispatch.failed":
        return "执行失败"
    if lowered == "dispatch.paused":
        return "执行已暂停"
    if lowered == "dispatch.resumed":
        return "执行已恢复"
    return "调度状态更新"


def _handoff_label(lowered: str) -> str:
    if lowered == "handoff.submitted":
        return "已提交交接"
    if lowered == "handoff.accepted":
        return "交接已接收"
    return "交接处理"


def _session_label(lowered: str) -> str:
    if lowered == "session.started":
        return "会话开始"
    if lowered == "session.resumed":
        return "会话恢复"
    if lowered == "session.forked":
        return "会话分叉"
    if lowered == "session.interrupted":
        return "会话中断"
    if lowered == "session.completed":
        return "会话结束"
    if lowered == "session.failed":
        return "会话失败"
    if lowered == "session.cancelled":
        return "会话取消"
    return "会话状态更新"


def _turn_label(lowered: str) -> str:
    if lowered == "turn.started":
        return "开始新一轮推理"
    if lowered == "turn.completed":
        return "本轮推理完成"
    if lowered == "turn.failed":
        return "本轮推理失败"
    if lowered == "turn.interrupted":
        return "本轮推理中断"
    return "推理轮次更新"


def _is_running_type(lowered: str) -> bool:
    if lowered.endswith(".started") or lowered.endswith(".call") or lowered.endswith(".tool_use"):
        return True
    if (
        lowered.endswith(".invoked")
        or lowered.endswith(".received")
        or lowered.endswith(".progress")
    ):
        return True
    if lowered.endswith(".gate_started") or lowered.endswith(".requested"):
        return True
    return False


def _is_completed_type(lowered: str) -> bool:
    if (
        lowered.endswith(".completed")
        or lowered.endswith(".result")
        or lowered.endswith(".tool_result")
    ):
        return True
    if (
        lowered.endswith(".applied")
        or lowered.endswith(".finished")
        or lowered.endswith(".uploaded")
    ):
        return True
    if lowered.endswith(".gate_finished") or lowered.endswith(".loaded"):
        return True
    return lowered == "completion_requested"


def _max_seq(sources: list[TraceSource], fallback: int) -> int:
    last = fallback
    seen = False
    for source in sources:
        if source.seq is None:
            continue
        if not seen or source.seq > last:
            last = source.seq
            seen = True
    return last


def _parse_detail(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or java_is_blank(raw):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _first_text(detail: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _text(detail, key)
        if value is not None:
            return value
    return None


def _text(detail: dict[str, Any], key: str) -> str | None:
    if key not in detail:
        return None
    value = detail[key]
    if value is None:
        return None
    rendered = _java_string(value)
    if java_is_blank(rendered):
        return None
    return rendered


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


def _java_trim(text: str) -> str:
    start = 0
    end = len(text)
    while start < end and ord(text[start]) <= 0x20:
        start += 1
    while end > start and ord(text[end - 1]) <= 0x20:
        end -= 1
    return text[start:end]
