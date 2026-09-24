"""定时任务能力快照。关闭时仍返回状态，不套用能力闸门。"""

from typing import Any

from fastapi import APIRouter, Depends

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.core.result import ok
from autowonder.scheduledtasks.capability import capability_snapshot

router = APIRouter(
    tags=["scheduled-task-capability"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看定时任务能力"))],
)


@router.get("/api/capabilities/scheduled-task")
async def scheduled_task_capability() -> dict[str, Any]:
    """当前部署下定时任务是否可用。"""
    return ok(capability_snapshot())
