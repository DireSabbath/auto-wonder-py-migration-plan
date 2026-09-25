"""把交互计划里的步骤提示对上 SDLC 步骤。"""

from collections.abc import Sequence


def resolve_step(
    steps: Sequence[object],
    tenant_id: int,
    step_id: str | None,
    hint: str | None,
) -> object | None:
    """先精确匹配 id、编码、名称或类型，唯一的部分匹配才采纳。都空则用入口步。"""
    normalized_id = ""
    if step_id is not None:
        normalized_id = step_id.strip()
    normalized_hint = ""
    if hint is not None:
        normalized_hint = hint.strip()
    owned = [step for step in steps if _owned(step, tenant_id)]
    for step in owned:
        if _exact(step, normalized_id, normalized_hint):
            return step
    if normalized_hint != "":
        needle = normalized_hint.lower()
        partial = [step for step in owned if _partial(step, needle)]
        if len(partial) == 1:
            return partial[0]
    if normalized_id == "" and normalized_hint == "":
        return _first(owned)
    return None


def _exact(step: object, step_id: str, hint: str) -> bool:
    identifier = str(_attr(step, "id"))
    code = _lower(_attr(step, "code"))
    if step_id != "" and (step_id == identifier or step_id.lower() == code):
        return True
    if hint == "":
        return False
    lowered = hint.lower()
    return (
        lowered == code
        or lowered == _lower(_attr(step, "name"))
        or lowered == _lower(_attr(step, "kind"))
    )


def _partial(step: object, needle: str) -> bool:
    return (
        _contains(_attr(step, "code"), needle)
        or _contains(_attr(step, "name"), needle)
        or _contains(_attr(step, "kind"), needle)
    )


def _contains(value: object, needle: str) -> bool:
    if not isinstance(value, str) or value.strip() == "":
        return False
    candidate = value.strip().lower()
    return needle in candidate or candidate in needle


def _first(steps: list[object]) -> object | None:
    chosen: object | None = None
    for step in steps:
        if chosen is None or _order(step) < _order(chosen):
            chosen = step
    return chosen


def _owned(step: object, tenant_id: int) -> bool:
    return _attr(step, "tenant_id") == tenant_id and _attr(step, "id") is not None


def _order(step: object) -> int:
    value = _attr(step, "step_order")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _lower(value: object) -> str:
    if isinstance(value, str):
        return value.lower()
    return ""


def _attr(step: object, name: str) -> object:
    return getattr(step, name)
