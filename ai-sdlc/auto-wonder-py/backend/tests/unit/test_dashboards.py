"""仪表盘汇总、工单语义 SQL 和路径。这些检查不访问数据库。"""

from datetime import datetime

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.dashboards.service import (
    build_health,
    build_inventory,
    build_kpi,
    build_squads,
    build_workstations,
    ensure_agent_present,
    format_generated_at,
    round1,
)
from autowonder.dashboards.sql import (
    COUNT_TODAY_COMPLETED_TASKS,
    COUNT_WEEK_COMPLETED_TASKS,
    END_TO_END_SUCCESSFUL_WORKITEMS,
    LIST_RUNNING_WORKITEMS,
    LIST_TODAY_COMPLETED_WORKITEMS,
    LIST_WEEK_COMPLETED_WORKITEMS,
    SOURCE_AGNOSTIC_SQL,
    WORKITEM_FENCED_SQL,
)
from autowonder.main import create_app


def test_kpi_load_duration_and_health_match_java() -> None:
    """负载、平均时长空值和成功率与 DashboardServiceTest 一致。"""
    loaded = build_kpi(7, 12, 57, 34, 0, 0, 0, 5)
    assert abs(loaded.avg_load - 1.4) < 0.001
    assert loaded.today_completed_tasks == 12
    assert loaded.week_completed_tasks == 57
    assert loaded.avg_task_duration_minutes == 34
    idle = build_kpi(3, 0, 0, None, 0, 0, 0, 0)
    assert idle.avg_load == 0.0
    assert idle.avg_task_duration_minutes == 0
    empty = build_health(0, 0, 0, None)
    assert empty.success_rate == 0.0
    assert empty.avg_duration_minutes == 0
    healthy = build_health(9, 1, 2, 8)
    assert abs(healthy.success_rate - 90.0) < 0.001
    assert abs(round1(2 / 3 * 100.0) - 66.7) < 0.001


def test_inventory_squads_and_workstations_match_java() -> None:
    """分类映射、小队负载和工位忙碌标记与 DashboardServiceTest 一致。"""
    inventory = build_inventory(
        [
            {"category": "INIT", "cnt": 9},
            {"category": "IN_PROGRESS", "cnt": 18},
            {"category": "DONE", "cnt": 32},
        ],
        [
            {"workType": "REQ", "cnt": 12},
            {"workType": "TASK", "cnt": 41},
            {"workType": "BUG", "cnt": 6},
        ],
    )
    assert inventory.by_lifecycle.init == 9
    assert inventory.by_lifecycle.in_progress == 18
    assert inventory.by_lifecycle.done == 32
    assert inventory.by_lifecycle.canceled == 0
    assert inventory.by_type.req == 12
    assert inventory.by_type.task == 41
    assert inventory.by_type.bug == 6
    squads = build_squads(
        [
            {
                "squadId": 10,
                "name": "A",
                "members": 0,
                "online": 0,
                "busy": 0,
                "runningTasks": 0,
            },
            {
                "squadId": 11,
                "name": "B",
                "members": 4,
                "online": 3,
                "busy": 2,
                "runningTasks": 6,
            },
        ],
        [{"squadId": 11, "cnt": 8}],
    )
    assert len(squads) == 2
    assert squads[0].load == 0.0
    assert abs(squads[1].load - 1.5) < 0.001
    assert squads[1].in_progress_workitems == 8
    assert squads[0].in_progress_workitems == 0
    stations = build_workstations(
        [
            {"agentId": 20, "name": "全栈-A", "avatarUrl": "", "runningTasks": 2},
            {"agentId": 21, "name": "测试-C", "avatarUrl": "", "runningTasks": 0},
        ]
    )
    assert stations[0].busy is True
    assert stations[1].busy is False
    assert format_generated_at(datetime(2026, 9, 23, 18, 4, 5)) == "2026-09-23 18:04:05"


def test_missing_agent_is_rejected() -> None:
    """数字员工不在当前工作空间时返回 AGENT_NOT_FOUND。"""
    ensure_agent_present(1)
    try:
        ensure_agent_present(0)
    except BizError as error:
        assert error.error_code == ErrorCode.AGENT_NOT_FOUND
    else:
        raise AssertionError("expected missing agent")


def test_workitem_sql_keeps_source_aware_fence() -> None:
    """工单查询带 source_type 围栏，全局调度指标不按来源过滤。"""
    definition = " ".join(END_TO_END_SUCCESSFUL_WORKITEMS.split())
    assert "FROM workitem w" in definition
    assert "sn.category = 'DONE'" in definition
    assert "w.assignee_type = 'HUMAN'" in definition
    assert "COALESCE(sn.category, '') <> 'DONE'" in definition
    assert "latest_dispatch.status = 'SUCCEEDED'" in definition
    assert "d.source_type = 'WORKITEM'" in definition
    for statement in WORKITEM_FENCED_SQL:
        assert "d.source_type = 'WORKITEM'" in statement
    for statement in SOURCE_AGNOSTIC_SQL:
        assert "source_type" not in statement
    today = " ".join(COUNT_TODAY_COMPLETED_TASKS.split())
    week = " ".join(COUNT_WEEK_COMPLETED_TASKS.split())
    assert "successful_workitem.success_at >= CURDATE()" in today
    assert "WEEKDAY(CURDATE())" in week
    today_list = " ".join(LIST_TODAY_COMPLETED_WORKITEMS.split())
    week_list = " ".join(LIST_WEEK_COMPLETED_WORKITEMS.split())
    running = " ".join(LIST_RUNNING_WORKITEMS.split())
    assert "workitemId" in today_list
    assert "title" in today_list
    assert "WEEKDAY(CURDATE())" in week_list
    assert "d.status = 'RUNNING'" in running
    assert "LIMIT" not in running


def test_dashboard_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/dashboard/realtime" in paths
    assert "/api/dashboard/agents/{id}/running" in paths
    assert "/api/dashboard/completed/today" in paths
    assert "/api/dashboard/completed/week" in paths
    assert "/api/dashboard/running" in paths
    response = client.get("/api/dashboard/realtime")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
