"""交接判定。这些函数不写库，入站帧和测试共用。"""

from collections.abc import Sequence
from dataclasses import dataclass

from autowonder.core.errors import BizError, ErrorCode

_TERMINAL_REWORK = frozenset({"FAILED", "TIMEOUT", "CANCELED"})
_HANDOFF_PREFIX = "handoff:"


@dataclass(frozen=True)
class HandoffResult:
    """执行器交接回执。``status`` 取 AGENT_DISPATCHED、HUMAN_ASSIGNED 或 REJECTED。"""

    status: str
    target_ref: int | None
    downstream_dispatch_id: int | None
    reason_code: str | None
    message: str | None


def agent_result(agent_id: int, dispatch_id: int) -> HandoffResult:
    """已经派给数字员工。"""
    return HandoffResult("AGENT_DISPATCHED", agent_id, dispatch_id, None, "handoff accepted")


def human_result(user_id: int, reason_code: str) -> HandoffResult:
    """已经交给真人。"""
    return HandoffResult("HUMAN_ASSIGNED", user_id, None, reason_code, "assigned to human")


def rejected_result(reason_code: str, message: str | None) -> HandoffResult:
    """交接没有写下下游。"""
    return HandoffResult("REJECTED", None, None, reason_code, message)


def handoff_target_type(status: str) -> str | None:
    """回执里的目标类型。拒绝时不写。"""
    if status == "AGENT_DISPATCHED":
        return "AGENT"
    if status == "HUMAN_ASSIGNED":
        return "HUMAN"
    return None


def handoff_source_id(idempotency_key: str | None) -> int | None:
    """从 ``handoff:{id}`` 取出上游调度。其他键没有上游。"""
    if idempotency_key is None or not idempotency_key.startswith(_HANDOFF_PREFIX):
        return None
    text = idempotency_key[len(_HANDOFF_PREFIX) :]
    if text == "" or not text.isdecimal():
        return None
    return int(text)


def automatic_handoff_limited(
    rows: Sequence[object],
    source_dispatch_id: int,
    target_agent_id: int,
    max_repeats: int,
) -> bool:
    """同一方向的自动交接达到上限。评论返工会把计数清零。"""
    if max_repeats <= 0:
        return True
    by_id: dict[int, object] = {}
    reset_boundary = 0
    for row in rows:
        row_id = _row_id(row)
        if row_id is None:
            continue
        by_id[row_id] = row
        if row_id <= source_dispatch_id and _text(row, "resume_mode") == "COMMENT_REWORK":
            reset_boundary = max(reset_boundary, row_id)
    source = by_id.get(source_dispatch_id)
    source_agent = None if source is None else _int_attr(source, "agent_id")
    if source is None or source_agent is None:
        return False
    repeats = 0
    for row in rows:
        row_id = _row_id(row)
        agent_id = None if row_id is None else _int_attr(row, "agent_id")
        if (
            row_id is None
            or row_id <= reset_boundary
            or row_id > source_dispatch_id
            or agent_id != target_agent_id
        ):
            continue
        parent_id = handoff_source_id(_text(row, "idempotency_key"))
        parent = None if parent_id is None else by_id.get(parent_id)
        parent_agent = None if parent is None else _int_attr(parent, "agent_id")
        if parent is not None and parent_agent == source_agent:
            repeats = repeats + 1
    return repeats >= max_repeats


def superseded_by_interaction_rework(rows: Sequence[object], source_dispatch_id: int) -> bool:
    """更新的评论返工还没失败时，旧调度不能再交接。"""
    for row in rows:
        row_id = _row_id(row)
        if row_id is None or row_id <= source_dispatch_id:
            continue
        if _text(row, "resume_mode") != "COMMENT_REWORK":
            continue
        if _text(row, "status") in _TERMINAL_REWORK:
            continue
        return True
    return False


def parse_human_ref(target: str | None) -> int | None:
    """真人目标只接受十进制用户 id。角色名不是用户。"""
    if target is None or target.strip() == "":
        return None
    text = target.strip()
    if not text.isdecimal():
        return None
    return int(text)


def scheduled_target_agent_id(snapshot: dict[str, object], target: str) -> int | None:
    """在冻结小队里按 id、名字或角色码找目标。"""
    contexts = snapshot.get("agentContexts")
    if not isinstance(contexts, list):
        return None
    for item in contexts:
        if not isinstance(item, dict):
            continue
        agent_id = item.get("agentId")
        if isinstance(agent_id, bool) or not isinstance(agent_id, int):
            continue
        identity = item.get("identity")
        name = None
        role_code = None
        if isinstance(identity, dict):
            name = identity.get("name")
            role_code = identity.get("roleCode")
        if target == str(agent_id):
            return agent_id
        if isinstance(name, str) and name.lower() == target.lower():
            return agent_id
        if isinstance(role_code, str) and role_code.lower() == target.lower():
            return agent_id
    return None


def frozen_entry_step(
    snapshot: dict[str, object], initial_agent_id: int, agent_id: int
) -> tuple[int | None, int | None]:
    """冻结 SDLC 的 id 和入口步骤。初始员工可以退回快照根上的 SDLC。"""
    sdlc = _context_sdlc(snapshot, agent_id)
    if sdlc is None and initial_agent_id == agent_id:
        root = snapshot.get("sdlc")
        if isinstance(root, dict):
            sdlc = root
    if sdlc is None and initial_agent_id != agent_id:
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "frozen target agent context is missing",
        )
    if sdlc is None:
        return None, None
    step_id = sdlc.get("currentStepId")
    sdlc_id = sdlc.get("id")
    if (
        isinstance(step_id, bool)
        or not isinstance(step_id, int)
        or step_id <= 0
        or isinstance(sdlc_id, bool)
        or not isinstance(sdlc_id, int)
        or sdlc_id <= 0
    ):
        raise BizError(
            ErrorCode.SCHEDULED_TASK_INVALID_STATE,
            "frozen target SDLC entry step is invalid",
        )
    return sdlc_id, step_id


def _context_sdlc(snapshot: dict[str, object], agent_id: int) -> dict[str, object] | None:
    contexts = snapshot.get("agentContexts")
    if not isinstance(contexts, list):
        return None
    for item in contexts:
        if not isinstance(item, dict) or item.get("agentId") != agent_id:
            continue
        sdlc = item.get("sdlc")
        if isinstance(sdlc, dict):
            return sdlc
        return None
    return None


def _row_id(row: object) -> int | None:
    return _int_attr(row, "id")


def _int_attr(row: object, name: str) -> int | None:
    value = getattr(row, name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(row: object, name: str) -> str | None:
    value = getattr(row, name)
    if isinstance(value, str):
        return value
    return None
