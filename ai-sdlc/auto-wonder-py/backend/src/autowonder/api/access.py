"""工作空间访问级别，rank 与 Java ``WorkspaceAccessLevel`` 一致。"""

from collections.abc import Awaitable, Callable
from enum import Enum

from autowonder.core.context import current
from autowonder.core.errors import ErrorCode, WorkspaceAccessDenied


class WorkspaceAccessLevel(Enum):
    """READ_ONLY < READ_WRITE < ADMIN。"""

    READ_ONLY = 0
    READ_WRITE = 1
    ADMIN = 2

    def allows(self, required: "WorkspaceAccessLevel") -> bool:
        """当前级别是否满足要求。"""
        return self.value >= required.value


def require_access(
    required: WorkspaceAccessLevel,
    action: str,
) -> Callable[[], Awaitable[WorkspaceAccessLevel]]:
    """FastAPI 依赖：当前请求的工作空间级别不足时拒绝。"""

    async def checker() -> WorkspaceAccessLevel:
        level_name = current().access_level
        if level_name is None:
            raise WorkspaceAccessDenied("NONE", required.name, action)
        level = WorkspaceAccessLevel[level_name]
        if not level.allows(required):
            raise WorkspaceAccessDenied(level.name, required.name, action)
        return level

    checker.__doc__ = f"要求工作空间访问级别 {required.name} 才能{action}。"
    return checker


def access_denied_body(error: WorkspaceAccessDenied) -> dict[str, str]:
    """拒绝详情，字段名与 Java VO 一致。"""
    return {
        "current": error.current,
        "required": error.required,
        "action": error.action,
    }


INSUFFICIENT = ErrorCode.WORKSPACE_ACCESS_INSUFFICIENT
