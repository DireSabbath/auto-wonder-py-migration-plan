"""集成能力、定时任务能力快照和执行器自动升级开关。"""

import pytest
from fastapi.testclient import TestClient

from autowonder.executors.updates import runtime_auto_update_view
from autowonder.main import create_app
from autowonder.scheduledtasks.capability import (
    CLUSTER_NOT_READY,
    FEATURE_DISABLED,
    SCHEMA_MODE_READY,
    capability_snapshot,
)


class _ScheduledFlags:
    def __init__(self, enabled: bool, cluster_ready: bool) -> None:
        self.scheduled_task_enabled = enabled
        self.scheduled_task_cluster_ready = cluster_ready


class _IntegrationFlags:
    def __init__(self, aone_enabled: bool) -> None:
        self.aone_enabled = aone_enabled


class _RuntimeFlags:
    def __init__(self, enabled: bool, version: str) -> None:
        self.executor_auto_update_enabled = enabled
        self.recommended_runtime_version = version


def test_integration_capabilities_are_public() -> None:
    """能力查询在白名单上，默认报告 Aone 关闭。"""
    client = TestClient(create_app())
    response = client.get("/api/integrations/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == {"aoneEnabled": False}


def test_integration_capabilities_follow_deployment_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """打开部署开关后，同一路径报告 Aone 启用。"""
    monkeypatch.setattr(
        "autowonder.integrations.capabilities.get_settings",
        lambda: _IntegrationFlags(True),
    )
    client = TestClient(create_app())
    response = client.get("/api/integrations/capabilities")
    assert response.status_code == 200
    assert response.json()["data"] == {"aoneEnabled": True}


def test_scheduled_capability_reason_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """schema 视为就绪时，模块关闭优先于集群未就绪。"""
    monkeypatch.setattr(
        "autowonder.scheduledtasks.capability.get_settings",
        lambda: _ScheduledFlags(False, False),
    )
    disabled = capability_snapshot()
    assert disabled.available is False
    assert disabled.mode == SCHEMA_MODE_READY
    assert disabled.cluster_ready is False
    assert disabled.reason == FEATURE_DISABLED

    monkeypatch.setattr(
        "autowonder.scheduledtasks.capability.get_settings",
        lambda: _ScheduledFlags(True, False),
    )
    cluster = capability_snapshot()
    assert cluster.available is False
    assert cluster.cluster_ready is False
    assert cluster.reason == CLUSTER_NOT_READY

    monkeypatch.setattr(
        "autowonder.scheduledtasks.capability.get_settings",
        lambda: _ScheduledFlags(True, True),
    )
    ready = capability_snapshot()
    assert ready.available is True
    assert ready.cluster_ready is True
    assert ready.mode == SCHEMA_MODE_READY
    assert ready.reason is None


def test_scheduled_capability_route_requires_login() -> None:
    """能力快照要求登录，且关闭时仍由接口返回，而不是闸门拒绝。"""
    client = TestClient(create_app())
    path = client.app.openapi()["paths"]["/api/capabilities/scheduled-task"]
    assert list(path) == ["get"]
    response = client.get("/api/capabilities/scheduled-task")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"


def test_runtime_auto_update_reads_deployment_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """面板只读部署开关和统一目标版本。"""
    monkeypatch.setattr(
        "autowonder.executors.updates.get_settings",
        lambda: _RuntimeFlags(True, "0.2.160"),
    )
    view = runtime_auto_update_view()
    assert view.executor_auto_update_enabled is True
    assert view.target_version == "0.2.160"


def test_runtime_auto_update_route_is_get_only() -> None:
    """自动升级开关没有写接口，未登录时拒绝。"""
    client = TestClient(create_app())
    path = client.app.openapi()["paths"]["/api/platform/runtime-auto-update"]
    assert list(path) == ["get"]
    response = client.get("/api/platform/runtime-auto-update")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
