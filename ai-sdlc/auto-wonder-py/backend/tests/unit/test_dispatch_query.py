"""调度查询的分页、来源、产物分类和路由。这些检查不访问数据库。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.artifacts.classification import (
    classify_artifact,
    resolve_artifact_type,
    user_visible,
)
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.query import (
    build_view,
    compute_since,
    days_from_range,
    execution_source_type,
    list_statement,
    page_window,
    require_same_tenant,
    workitem_lookup_ids,
)
from autowonder.main import create_app


def test_page_window_clamps_like_java() -> None:
    """页码小于 1 从第一页起，每页至少 1 条、至多 100 条。"""
    assert page_window(2, 50) == (2, 50, 50)
    assert page_window(1, 9999) == (1, 100, 0)
    assert page_window(-5, 50) == (1, 50, 0)
    assert page_window(1, 0) == (1, 1, 0)


def test_time_range_keeps_clock_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """7 天、90 天，其余 30 天，并且保留当前钟点。"""
    monkeypatch.setattr(
        "autowonder.dispatch.query.now_local",
        lambda: datetime(2026, 7, 15, 8, 30, 1),
    )
    assert days_from_range("7d") == 7
    assert days_from_range("90d") == 90
    assert days_from_range("30d") == 30
    assert days_from_range("other") == 30
    assert days_from_range(None) == 30
    assert compute_since("7d") == datetime(2026, 7, 8, 8, 30, 1)


def test_list_sql_filters_workitems_and_orders_newest_first() -> None:
    """工单筛选同时限定来源。空状态不进条件。分页用 MySQL 的 limit 偏移。"""
    since = datetime(2026, 6, 15, 8, 30, 1)
    plain = _sql(list_statement(100, "", None, None, since, 2, 50))
    assert "dispatch.tenant_id = 100" in plain
    assert "dispatch.is_deleted = 0" in plain
    assert "AND dispatch.status" not in plain
    assert "AND dispatch.agent_id" not in plain
    assert "AND dispatch.workitem_id" not in plain
    assert "ORDER BY dispatch.gmt_create DESC" in plain
    assert "LIMIT 50, 50" in plain
    filtered = _sql(list_statement(100, "RUNNING", 20, 10, since, 1, 9999))
    assert "dispatch.status = 'RUNNING'" in filtered
    assert "dispatch.agent_id = 20" in filtered
    assert "dispatch.source_type = 'WORKITEM'" in filtered
    assert "dispatch.workitem_id = 10" in filtered
    assert "LIMIT 0, 100" in filtered


def test_source_type_blank_means_workitem_and_unknown_raises() -> None:
    """空来源按工单。未知枚举沿用 Java 的枚举错误。"""
    assert execution_source_type(None) == "WORKITEM"
    assert execution_source_type("   ") == "WORKITEM"
    assert execution_source_type("SCHEDULED_TASK_RUN") == "SCHEDULED_TASK_RUN"
    with pytest.raises(ValueError, match="ExecutionSourceType.NOPE"):
        execution_source_type("NOPE")


def test_scheduled_run_does_not_look_up_a_workitem_title() -> None:
    """定时任务运行 id 不拿去查工单标题。"""
    scheduled = _row(source_type="SCHEDULED_TASK_RUN", workitem_id=10)
    assert workitem_lookup_ids([scheduled]) == []
    view = build_view(scheduled, {10: "不应出现"}, {20: "前端开发"}, {30: 7}, {40: "dev-01"})
    assert view.source_type == "SCHEDULED_TASK_RUN"
    assert view.workitem_title is None
    assert view.agent_name == "前端开发"
    assert view.agent_version_no == 7
    assert view.executor_name == "dev-01"
    assert view.artifacts is None


def test_workitem_dispatch_uses_titles_and_rejects_other_tenants() -> None:
    """工单来源补标题。其他工作空间与缺失行都是调度不存在。"""
    row = _row(source_type="WORKITEM", workitem_id=10)
    assert workitem_lookup_ids([row]) == [10]
    view = build_view(row, {10: "登录页重构"}, {20: "前端开发"}, {}, {})
    assert view.source_type == "WORKITEM"
    assert view.workitem_title == "登录页重构"
    assert view.agent_version_no is None
    assert view.executor_name is None
    other = _row(tenant_id=200)
    with pytest.raises(BizError) as caught:
        require_same_tenant(other, 100)
    assert caught.value.code == ErrorCode.DISPATCH_NOT_FOUND.code
    with pytest.raises(BizError):
        require_same_tenant(None, 100)


def test_artifact_classification_matches_java_vectors() -> None:
    """路径分类和 FILE 补全与 Java 向量一致。"""
    assert classify_artifact("artifacts/output/deliverables/report.md") == "DELIVERABLE"
    assert classify_artifact("./output/evidence/report.md") == "EVIDENCE"
    assert classify_artifact("artifacts\\output\\handoff\\summary.md") == "HANDOFF"
    snapshot = "artifacts/attempts/step-1/attempt-2/artifacts/output/deliverables/report.md"
    assert classify_artifact(snapshot) == "SNAPSHOT"
    assert classify_artifact("result/runtime-result.json") == "RUNTIME"
    assert classify_artifact("debug/RD-10.log.gz") == "DEBUG_LOG"
    assert classify_artifact("custom/deliverables/report.md") == "FILE"
    assert classify_artifact(None) == "FILE"
    assert resolve_artifact_type("FUTURE_TYPE", "deliverables/report.md") == "FUTURE_TYPE"
    assert resolve_artifact_type("REPORT", "deliverables/report.md") == "REPORT"
    assert resolve_artifact_type(None, "patches/change.patch") == "PATCH"
    assert resolve_artifact_type("FILE", "learning_delta/memory_delta.json") == "LEARNING"
    assert resolve_artifact_type("", "custom/new-format.bin") == "FILE"
    assert resolve_artifact_type(" file ", "patches/change.patch") == "PATCH"
    assert user_visible(None) is True
    assert user_visible("deliverables/report.md") is True
    assert user_visible("observability/trace.json") is False
    assert user_visible("output/observability/trace.json") is False


def test_dispatch_routes_match_java_and_require_login() -> None:
    """列表和详情已注册，未登录返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "get" in paths["/api/dispatches"]
    assert "get" in paths["/api/dispatches/{id}"]
    response = client.get("/api/dispatches")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    detail = client.get("/api/dispatches/1")
    assert detail.status_code == 401
    assert detail.json()["code"] == "10401"


def _sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)


def _row(
    source_type: str = "WORKITEM",
    workitem_id: int = 10,
    tenant_id: int = 100,
) -> Dispatch:
    return Dispatch(
        id=1,
        tenant_id=tenant_id,
        source_type=source_type,
        workitem_id=workitem_id,
        agent_id=20,
        agent_version_id=30,
        executor_id=40,
        status="SUCCEEDED",
        attempt=1,
        idempotency_key="workitem-10",
        gmt_create=datetime(2026, 1, 2, 3, 4, 5),
        gmt_modified=datetime(2026, 1, 2, 3, 4, 5),
    )
