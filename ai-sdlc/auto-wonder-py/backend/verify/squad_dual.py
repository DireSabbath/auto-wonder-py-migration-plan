"""在两个正在跑的 API 上走完七角色小队到交真人。

页面时间线仍由 ``squad-flow`` 对着 Python 前端核对。
这里只比较两边的角色、派发、交真人、负责人和时间线。
"""

from verify.squad_flow import squad_flow

_KEYS = (
    "appliedRoles",
    "dispatchStatus",
    "handoffStatus",
    "handoffTargetType",
    "assigneeType",
    "timelineEvents",
    "unifiedPhrases",
)


def squad_dual(python_url: str, java_url: str) -> dict[str, object]:
    """比较两个栈交真人之后的可观察结果。"""
    java = squad_flow(java_url, include_page=False)
    python = squad_flow(python_url, include_page=False)
    java_view = _view(java)
    python_view = _view(python)
    same = java.get("ok") is True and python.get("ok") is True and java_view == python_view
    checks = _prefixed("java", java) + _prefixed("python", python)
    checks.append(
        {
            "name": "outcomes",
            "pass": same,
            "note": "roles, dispatch, handoff and timeline match",
        }
    )
    passed = 0
    failed = 0
    for item in checks:
        if item["pass"] is True:
            passed += 1
        else:
            failed += 1
    body: dict[str, object] = {
        "command": "squad-dual",
        "ok": failed == 0,
        "passCount": passed,
        "failCount": failed,
        "checks": checks,
        "facts": {"java": java_view, "python": python_view},
    }
    if java.get("stopped") is not None:
        body["javaStopped"] = java["stopped"]
    if python.get("stopped") is not None:
        body["pythonStopped"] = python["stopped"]
    return body


def _view(verdict: dict[str, object]) -> dict[str, object]:
    facts = verdict.get("facts")
    view: dict[str, object] = {}
    if not isinstance(facts, dict):
        return view
    for key in _KEYS:
        if key in facts:
            view[key] = facts[key]
    return view


def _prefixed(name: str, verdict: dict[str, object]) -> list[dict[str, object]]:
    checks = verdict.get("checks")
    copied: list[dict[str, object]] = []
    if not isinstance(checks, list):
        return copied
    for item in checks:
        if isinstance(item, dict):
            copied.append(
                {
                    "name": name + "_" + str(item.get("name")),
                    "pass": item.get("pass") is True,
                    "note": item.get("note"),
                }
            )
    return copied
