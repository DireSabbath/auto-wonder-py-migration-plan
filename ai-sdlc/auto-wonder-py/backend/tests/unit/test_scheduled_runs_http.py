"""定时任务立即运行、文档和运行记录的路径与状态规则。"""

import pytest
from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app
from autowonder.scheduledtasks.runs import (
    can_transition,
    determine_step_status,
)
from autowonder.scheduledtasks.runs import (
    router as run_router,
)
from autowonder.scheduledtasks.trigger import manual_key

_PATHS = {
    "/api/scheduled-tasks/{id}/run-now": {"post"},
    "/api/scheduled-tasks/{id}/documents": {"get", "post"},
    "/api/scheduled-tasks/{id}/documents/{artifactId}": {"delete"},
    "/api/scheduled-task-runs/{runId}": {"get"},
    "/api/scheduled-task-runs/{runId}/pause": {"post"},
    "/api/scheduled-task-runs/{runId}/resume": {"post"},
    "/api/scheduled-task-runs/{runId}/cancel": {"post"},
    "/api/scheduled-task-runs/{runId}/comments": {"get", "post"},
    "/api/scheduled-task-runs/{runId}/mention-candidates": {"get"},
    "/api/scheduled-task-runs/{runId}/artifacts": {"get"},
    "/api/scheduled-task-runs/{runId}/events": {"get"},
    "/api/scheduled-task-runs/{runId}/derived-workitems": {"get"},
    "/api/scheduled-task-runs/{runId}/participants": {"get"},
    "/api/scheduled-task-runs/{runId}/delivery-progress": {"get"},
}


class _Event:
    def __init__(self, event_type: str, step_id: int | None) -> None:
        self.event_type = event_type
        self.step_id = step_id
        self.detail_json = None
        self.event_time = None


def _client() -> TestClient:
    app = create_app()
    app.include_router(run_router)
    return TestClient(app)


def test_scheduled_run_routes_exist() -> None:
    """运行、文档和立即运行的路径都在。"""
    paths = _client().app.openapi()["paths"]
    for path, methods in _PATHS.items():
        assert methods <= set(paths[path])


def test_scheduled_run_routes_require_login() -> None:
    """未带令牌时返回 401，业务码 10401。"""
    client = _client()
    response = client.post("/api/scheduled-tasks/1/run-now", json={"requestId": "r", "version": 1})
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    detail = client.get("/api/scheduled-task-runs/1")
    assert detail.status_code == 401
    assert detail.json()["code"] == "10401"
    documents = client.get("/api/scheduled-tasks/1/documents")
    assert documents.status_code == 401
    assert documents.json()["code"] == "10401"


def test_run_transition_rules() -> None:
    """暂停、恢复和取消只允许 Java 里的来源状态。"""
    assert can_transition("RUNNING", "PAUSED") is True
    assert can_transition("PAUSED", "QUEUED") is True
    assert can_transition("QUEUED", "CANCELED") is True
    assert can_transition("SUCCEEDED", "CANCELED") is False
    assert can_transition("PAUSED", "PAUSED") is False


def test_manual_key_trims_request_id() -> None:
    """手动幂等键去掉两端空白，空值拒绝。"""
    assert manual_key(9, "  abc ") == "task:9:manual:abc"
    with pytest.raises(BizError) as caught:
        manual_key(9, "  ")
    assert caught.value.error_code == ErrorCode.SCHEDULED_TASK_VALIDATION_FAILED


def test_step_status_prefers_completion() -> None:
    """完成优先于失败和运行中；只开始过的暂停运行显示 paused。"""
    events = [_Event("step.started", 3), _Event("step.completed", 3), _Event("step.failed", 3)]
    assert determine_step_status(3, events, "RUNNING") == "done"
    started = [_Event("step.started", 3)]
    assert determine_step_status(3, started, "PAUSED") == "paused"
    assert determine_step_status(3, [], "RUNNING") == "pending"
