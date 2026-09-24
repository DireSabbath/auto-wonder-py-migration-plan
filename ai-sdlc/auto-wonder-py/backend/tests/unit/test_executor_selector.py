"""执行器选择的偏好、轮询和失败原因。这些检查不连接 Redis。"""

import pytest

from autowonder.dispatch.selector import (
    ExecutorView,
    ProtocolCompatibilityError,
    choose_strict,
    diagnose_unavailable,
    plan_selection,
    round_robin_pick,
    waiting_error,
    waiting_retryable,
)
from autowonder.executors.registry import DispatchSnapshot


def _view(
    executor_id: int,
    capacity: int = 2,
    occupying: frozenset[int] = frozenset(),
    features: frozenset[str] = frozenset(),
    available: bool = True,
    online: bool = True,
    ready: bool = True,
    error: str | None = None,
    snapshot: bool = True,
) -> ExecutorView:
    current = None
    if snapshot:
        current = DispatchSnapshot(
            capacity=capacity,
            authoritative_inventory=True,
            inventory_ready=ready,
            inventory_error=error,
            running_dispatch_ids=frozenset(),
            running_conversation_turn_ids=frozenset(),
            owned_dispatch_ids=frozenset(),
        )
    return ExecutorView(available, online, current, features, occupying)


def test_preferred_executor_wins_when_it_has_capacity() -> None:
    """偏好执行器有容量时不轮询其他人。"""
    views = {10: _view(10), 20: _view(20)}
    chosen, eligible = plan_selection({"10", "20"}, views, 10, False, None)
    assert chosen == 10
    assert eligible == []


def test_round_robin_skips_a_full_preferred_executor() -> None:
    """偏好执行器占满普通槽位后，轮询落到下一台。交互派发仍可用满容量。"""
    views = {
        10: _view(10, occupying=frozenset({1})),
        20: _view(20),
    }
    chosen, eligible = plan_selection({"10", "20"}, views, 10, False, None)
    assert chosen is None
    assert eligible == [20]
    assert round_robin_pick(eligible, 1) == 20
    assert round_robin_pick([10, 20, 30], 3) == 30
    assert round_robin_pick([10, 20], 0) == 10
    interaction, rest = plan_selection({"10"}, views, 10, True, None)
    assert interaction == 10
    assert rest == []


def test_missing_protocol_feature_is_rejected() -> None:
    """有容量但没有要求的协议时，选择失败而不是改派。"""
    views = {10: _view(10, features=frozenset())}
    with pytest.raises(ProtocolCompatibilityError, match="AGENT_ENVIRONMENT_VARIABLES_V1"):
        plan_selection({"10"}, views, None, False, "AGENT_ENVIRONMENT_VARIABLES_V1")
    with pytest.raises(ProtocolCompatibilityError, match="AGENT_ENVIRONMENT_VARIABLES_V1"):
        choose_strict({"10"}, views[10], 10, "AGENT_ENVIRONMENT_VARIABLES_V1")


def test_strict_selection_stays_on_the_requested_executor() -> None:
    """连续会话不接受集合外或容量已满的执行器。"""
    view = _view(10)
    assert choose_strict({"10"}, view, 10, None) == 10
    assert choose_strict({"20"}, view, 10, None) is None
    full = _view(10, occupying=frozenset({1}))
    assert choose_strict({"10"}, full, 10, None) is None


def test_unavailable_reason_names_the_blocking_condition() -> None:
    """空集合、恢复中和容量耗尽分别对应 Java 的等待原因。"""
    assert diagnose_unavailable(set(), {}, None) == "NO_EXECUTOR_ONLINE"
    assert diagnose_unavailable(set(), {}, "protocol") == "RUNTIME_INCOMPATIBLE"
    recovering = {10: _view(10, ready=False)}
    assert diagnose_unavailable({"10"}, recovering, None) == "EXECUTOR_RECOVERING"
    full = {10: _view(10, occupying=frozenset({1}))}
    assert diagnose_unavailable({"10"}, full, None) == "NO_EXECUTOR_CAPACITY"
    assert waiting_retryable("NO_EXECUTOR_ONLINE") is True
    assert waiting_retryable("RUNTIME_INCOMPATIBLE") is False
    assert waiting_error("NO_EXECUTOR_ONLINE") == "NO_EXECUTOR_ONLINE: 等待执行器上线"
