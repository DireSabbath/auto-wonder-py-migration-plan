"""从派发运行时事件推导每个 SDLC 步骤的状态、子步骤和耗时。

事件桶保持到达顺序。最后一条事件依赖这个顺序，排序只发生在副本上。
"""

from datetime import datetime

from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.dispatch.action_text import looks_like_mojibake
from autowonder.dispatch.models import DispatchRuntimeEvent
from autowonder.evolution.jsontext import java_trim
from autowonder.sdlcs.models import SdlcStep
from autowonder.workitems.schemas import SubStepView
from autowonder.workitems.view import shanghai_millis

_TERMINAL_EVENT_TYPES = frozenset({"step.completed", "step.failed", "step.reused", "step.stale"})
_LONG_MIN = -9223372036854775808


class RuntimeStepTimeline:
    """一次派发里，按步骤序号归并后的运行时时间线。"""

    def __init__(
        self,
        events_by_order: dict[int, list[DispatchRuntimeEvent]],
        latest_order: int | None,
        latest_event_type: str | None,
        duration_by_order: dict[int, int],
    ) -> None:
        self._events_by_order = events_by_order
        self._latest_order = latest_order
        self._latest_event_type = latest_event_type
        self._duration_by_order = duration_by_order

    @classmethod
    def from_events(
        cls,
        events: list[DispatchRuntimeEvent],
        steps: list[SdlcStep],
        now: datetime,
    ) -> "RuntimeStepTimeline":
        """按步骤序号分桶，并算出每个已开始步骤的区间耗时。"""
        steps_by_order: dict[int, SdlcStep] = {}
        order_by_step_id: dict[int, int] = {}
        order_by_name: dict[str, int] = {}
        order_by_code: dict[str, int] = {}
        for step in steps:
            if step.step_order is None:
                continue
            if step.step_order not in steps_by_order:
                steps_by_order[step.step_order] = step
            if step.id is not None and step.id not in order_by_step_id:
                order_by_step_id[step.id] = step.step_order
            if step.name is not None and java_trim(step.name) not in order_by_name:
                order_by_name[java_trim(step.name)] = step.step_order
            if step.code is not None and not java_is_blank(step.code):
                code = java_trim(step.code)
                if code not in order_by_code:
                    order_by_code[code] = step.step_order
        by_order: dict[int, list[DispatchRuntimeEvent]] = {}
        latest_order: int | None = None
        latest_type: str | None = None
        for event in events:
            order = _resolve_order(
                event, steps_by_order, order_by_step_id, order_by_name, order_by_code
            )
            if order is None:
                continue
            bucket = by_order.get(order)
            if bucket is None:
                bucket = []
                by_order[order] = bucket
            bucket.append(event)
            previous = None
            if latest_order is not None:
                previous = _last(by_order.get(latest_order))
            if latest_order is None or _compare_event(event, previous) >= 0:
                latest_order = order
                latest_type = event.event_type
        return cls(by_order, latest_order, latest_type, _durations(by_order, now))

    def status_of(self, step: SdlcStep) -> str | None:
        """该步骤相对最新事件的执行状态。没有可定位的事件时为空。"""
        if self._latest_order is None or step.step_order is None:
            return None
        order = step.step_order
        last = _last(self._events_by_order.get(order))
        if last is not None and last.event_type == "step.reused":
            return "done"
        if last is not None and last.event_type == "step.stale":
            return "pending"
        if last is not None and _is_failure_event(last):
            return "failed"
        if order < self._latest_order:
            return "done"
        if order > self._latest_order:
            return "pending"
        if _is_completion_event(self._latest_event_type):
            return "done"
        return "active"

    def is_current(self, step: SdlcStep) -> bool:
        """该步骤是否就是最新一条可定位事件所在的步骤。"""
        if self._latest_order is None:
            return False
        return self._latest_order == step.step_order

    def sub_steps_of(self, step: SdlcStep, step_status: str) -> list[SubStepView]:
        """按到达顺序展开子步骤。失败事件保持失败，最后一条跟随步骤状态。"""
        if step.step_order is None:
            return []
        events = self._events_by_order.get(step.step_order)
        if events is None or len(events) == 0:
            return []
        result: list[SubStepView] = []
        last_index = len(events) - 1
        for index, event in enumerate(events):
            status = "done"
            if _is_failure_event(event):
                status = "failed"
            elif index == last_index and step_status in {"active", "paused", "failed"}:
                status = step_status
            result.append(SubStepView(name=_label_of(event), status=status))
        return result

    def last_event_of(self, step: SdlcStep) -> DispatchRuntimeEvent | None:
        """该步骤桶里按到达顺序的最后一条事件。"""
        if step.step_order is None:
            return None
        return _last(self._events_by_order.get(step.step_order))

    def duration_of(self, step: SdlcStep) -> int | None:
        """只统计见过 ``step.started`` 的步骤。没有启动事件时为空。"""
        if step.step_order is None:
            return None
        return self._duration_by_order.get(step.step_order)


def _resolve_order(
    event: DispatchRuntimeEvent,
    steps_by_order: dict[int, SdlcStep],
    order_by_step_id: dict[int, int],
    order_by_name: dict[str, int],
    order_by_code: dict[str, int],
) -> int | None:
    if event.step_order is not None and event.step_order in steps_by_order:
        return event.step_order
    if event.step_id is not None and event.step_id in order_by_step_id:
        return order_by_step_id[event.step_id]
    if event.step_key is not None:
        order = order_by_code.get(java_trim(event.step_key))
        if order is not None:
            return order
    if event.step_name is not None:
        order = order_by_name.get(java_trim(event.step_name))
        if order is not None:
            return order
    return None


def _compare_event(left: DispatchRuntimeEvent, right: DispatchRuntimeEvent | None) -> int:
    if right is None:
        return 1
    if left.id is not None and right.id is not None:
        if left.id > right.id:
            return 1
        if left.id < right.id:
            return -1
        return 0
    if left.gmt_create is not None and right.gmt_create is not None:
        if left.gmt_create > right.gmt_create:
            return 1
        if left.gmt_create < right.gmt_create:
            return -1
    return 0


def _last(events: list[DispatchRuntimeEvent] | None) -> DispatchRuntimeEvent | None:
    if events is None or len(events) == 0:
        return None
    return events[len(events) - 1]


def _is_completion_event(event_type: str | None) -> bool:
    return event_type in {
        "step.completed",
        "step.completion_requested",
        "completion_requested",
        "dispatch.completed",
    }


def _is_failure_event(event: DispatchRuntimeEvent) -> bool:
    return (
        event.error is not None
        or event.event_type == "step.failed"
        or event.event_type == "dispatch.failed"
    )


def _label_of(event: DispatchRuntimeEvent) -> str:
    if event.message is not None and not java_is_blank(event.message):
        if not looks_like_mojibake(event.message):
            return event.message
    labeled = _runtime_event_label(event.event_type)
    if labeled is not None:
        return labeled
    if event.event_type is not None:
        return event.event_type
    return "运行进度"


def _runtime_event_label(event_type: str | None) -> str | None:
    if event_type is None:
        return None
    if event_type.startswith("step.started"):
        return "开始执行"
    if _is_completion_event(event_type):
        return "请求完成"
    if event_type == "step.gate_started":
        return "开始校验"
    if event_type == "step.gate_finished":
        return "校验完成"
    if event_type == "step.fix_required":
        return "需要修复"
    if event_type == "step.failed" or event_type == "dispatch.failed":
        return "执行失败"
    return None


def _at(event: DispatchRuntimeEvent) -> datetime | None:
    if event.event_time is not None:
        return event.event_time
    return event.gmt_create


def _positive_delta(start: datetime | None, end: datetime | None) -> int:
    if start is None or end is None:
        return 0
    delta = shanghai_millis(end) - shanghai_millis(start)
    if delta > 0:
        return delta
    return 0


def _duration_sort_key(event: DispatchRuntimeEvent) -> tuple[int, int]:
    event_id = _LONG_MIN
    if event.id is not None:
        event_id = event.id
    moment = _at(event)
    millis = _LONG_MIN
    if moment is not None:
        millis = shanghai_millis(moment)
    return (event_id, millis)


def _durations(
    events_by_order: dict[int, list[DispatchRuntimeEvent]], now: datetime
) -> dict[int, int]:
    durations: dict[int, int] = {}
    for order, bucket in events_by_order.items():
        total = 0
        open_start: datetime | None = None
        saw_started = False
        ordered = list(bucket)
        ordered.sort(key=_duration_sort_key)
        for event in ordered:
            event_type = event.event_type
            if event_type == "step.started":
                saw_started = True
                if open_start is None:
                    open_start = _at(event)
            elif event_type in _TERMINAL_EVENT_TYPES and open_start is not None:
                total = total + _positive_delta(open_start, _at(event))
                open_start = None
        if open_start is not None:
            total = total + _positive_delta(open_start, now)
        if saw_started:
            durations[order] = total
    return durations
