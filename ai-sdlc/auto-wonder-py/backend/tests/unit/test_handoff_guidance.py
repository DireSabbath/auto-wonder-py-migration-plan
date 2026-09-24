"""交接计数、步骤提示和定时目标解析。不连接数据库。"""

from types import SimpleNamespace

import pytest

from autowonder.core.errors import BizError
from autowonder.dispatch.handoff_rules import (
    automatic_handoff_limited,
    frozen_entry_step,
    handoff_source_id,
    handoff_target_type,
    parse_human_ref,
    scheduled_target_agent_id,
    superseded_by_interaction_rework,
)
from autowonder.guidance.steps import resolve_step


def _row(
    row_id: int,
    agent_id: int,
    key: str,
    resume_mode: str | None = None,
    status: str = "SUCCEEDED",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        agent_id=agent_id,
        idempotency_key=key,
        resume_mode=resume_mode,
        status=status,
    )


def test_automatic_handoff_limit_counts_same_direction() -> None:
    """同一上游员工交给同一下游已有五次时，下一次自动交接达到上限。"""
    upstream = [_row(1, 10, "1:1:1")]
    edges = [_row(index, 20, "handoff:1") for index in range(2, 7)]
    current = _row(7, 10, "current")
    assert automatic_handoff_limited([*upstream, *edges, current], 7, 20, 5) is True
    assert automatic_handoff_limited([*upstream, *edges[:4], current], 7, 20, 5) is False


def test_comment_rework_resets_automatic_handoff_count() -> None:
    """评论返工之前的同方向交接不计入当前段落。"""
    edges = [_row(index, 20, "handoff:1") for index in range(2, 7)]
    reset = [
        _row(1, 10, "1:1:1"),
        *edges,
        _row(7, 10, "rework", resume_mode="COMMENT_REWORK"),
        _row(8, 20, "handoff:7"),
        _row(9, 10, "current"),
    ]
    assert automatic_handoff_limited(reset, 9, 20, 5) is False
    continued = [
        _row(1, 10, "1:1:1"),
        *edges,
        _row(8, 20, "handoff:1"),
        _row(9, 10, "current"),
    ]
    assert automatic_handoff_limited(continued, 9, 20, 5) is True


def test_newer_comment_rework_supersedes_handoff() -> None:
    """还没失败的更新返工挡住旧调度的交接。"""
    rows = [
        _row(4, 10, "1:1:1"),
        _row(9, 10, "interaction-rework:8", resume_mode="COMMENT_REWORK", status="PENDING"),
    ]
    assert superseded_by_interaction_rework(rows, 4) is True
    rows[1].status = "CANCELED"
    assert superseded_by_interaction_rework(rows, 4) is False


def test_handoff_keys_and_human_ref() -> None:
    """幂等键、回执类型和真人 id 的解析。"""
    assert handoff_source_id("handoff:18") == 18
    assert handoff_source_id("guidance:18") is None
    assert handoff_source_id("handoff:1a") is None
    assert handoff_target_type("AGENT_DISPATCHED") == "AGENT"
    assert handoff_target_type("HUMAN_ASSIGNED") == "HUMAN"
    assert handoff_target_type("REJECTED") is None
    assert parse_human_ref(" 42 ") == 42
    assert parse_human_ref("reviewer") is None
    assert parse_human_ref(None) is None


def test_scheduled_target_matches_id_name_or_role() -> None:
    """冻结小队按数字 id、名字或角色码匹配，大小写不敏感。"""
    snapshot = {
        "agentContexts": [
            {"agentId": 7, "identity": {"name": "编码", "roleCode": "CODER"}},
        ]
    }
    assert scheduled_target_agent_id(snapshot, "7") == 7
    assert scheduled_target_agent_id(snapshot, "编码") == 7
    assert scheduled_target_agent_id(snapshot, "coder") == 7
    assert scheduled_target_agent_id(snapshot, "reviewer") is None


def test_frozen_entry_step_uses_context_sdlc() -> None:
    """目标员工自己的冻结 SDLC 提供入口步骤。"""
    snapshot = {
        "agentContexts": [
            {"agentId": 7, "sdlc": {"id": 3, "currentStepId": 11}},
        ]
    }
    assert frozen_entry_step(snapshot, 1, 7) == (3, 11)


def test_frozen_entry_step_rejects_missing_context() -> None:
    """不是初始员工、又没有自己的 SDLC 时，快照不能交接。"""
    with pytest.raises(BizError, match="frozen target agent context is missing"):
        frozen_entry_step({"agentContexts": []}, 1, 7)


def test_resolve_step_exact_partial_and_entry() -> None:
    """精确命中优先。提示唯一地包含在名称里时采纳。都空则用最小步序。"""
    steps = [
        SimpleNamespace(id=2, tenant_id=1, code="dev", name="实现", kind="WORK", step_order=2),
        SimpleNamespace(id=1, tenant_id=1, code="analyze", name="分析", kind="WORK", step_order=1),
        SimpleNamespace(id=9, tenant_id=2, code="other", name="其他", kind="WORK", step_order=0),
    ]
    assert resolve_step(steps, 1, "1", None) is steps[1]
    assert resolve_step(steps, 1, None, "实现") is steps[0]
    assert resolve_step(steps, 1, None, "分") is steps[1]
    assert resolve_step(steps, 1, None, None) is steps[1]
    assert resolve_step(steps, 1, "missing", None) is None
