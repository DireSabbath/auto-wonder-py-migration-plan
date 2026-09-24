"""Daemon 上传鉴权。调度不存在或执行器令牌不对时拒绝。"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.dispatch.models import Dispatch, DispatchRecovery
from autowonder.dispatch.query import execution_source_type
from autowonder.executors.models import Executor
from autowonder.executors.tokens import validate
from autowonder.workitems.models import WorkitemExecutionControl

_INTERACTION_MODES = frozenset(
    {"COMMENT_INTERACTION", "SIDE_INTERACTION", "CANONICAL_INTERACTION"}
)


@dataclass
class UploadAuth:
    """一次 daemon 上传的身份。失败时数值字段保持 0。"""

    success: bool
    tenant_id: int
    workitem_id: int
    agent_id: int
    resume_mode: str | None
    source_type: str

    def interaction(self) -> bool:
        """评论、旁路和规范交互都属于交互调度。"""
        if self.resume_mode is None:
            return False
        return self.resume_mode.upper() in _INTERACTION_MODES


def authenticate_loaded(
    dispatch: Dispatch | None,
    executor: Executor | None,
    token: str | None,
) -> UploadAuth:
    """用已经读出的调度和执行器完成与 Java 相同的校验。"""
    if dispatch is None or executor is None or not validate(executor.token_ref, token):
        return UploadAuth(False, 0, 0, 0, None, "WORKITEM")
    return UploadAuth(
        True,
        dispatch.tenant_id,
        dispatch.workitem_id,
        dispatch.agent_id,
        dispatch.resume_mode,
        execution_source_type(dispatch.source_type),
    )


def mutation_fenced(
    dispatch: Dispatch | None,
    cancel_requested: bool,
    workitem_closed: bool,
) -> bool:
    """取消、已请求停止，或工单已关闭时，不能再写业务产物。

    检查点上传不走这道栅栏。调用方传入停止标记和工单是否关闭。
    """
    if dispatch is None or dispatch.status == "CANCELED" or cancel_requested:
        return True
    return execution_source_type(dispatch.source_type) == "WORKITEM" and workitem_closed


async def load_mutation_fence(session: AsyncSession, dispatch_id: int) -> bool:
    """取消请求、调度已取消，或工单交付已关闭时，业务产物不能再写。"""
    dispatch = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    cancel_requested = False
    workitem_closed = False
    if dispatch is not None:
        recovery = await session.scalar(
            select(DispatchRecovery)
            .where(
                DispatchRecovery.tenant_id == dispatch.tenant_id,
                DispatchRecovery.dispatch_id == dispatch.id,
                DispatchRecovery.cancel_requested == 1,
            )
            .limit(1)
        )
        cancel_requested = recovery is not None
        if execution_source_type(dispatch.source_type) == "WORKITEM":
            control = await session.scalar(
                select(WorkitemExecutionControl)
                .where(
                    WorkitemExecutionControl.tenant_id == dispatch.tenant_id,
                    WorkitemExecutionControl.workitem_id == dispatch.workitem_id,
                    WorkitemExecutionControl.closed == 1,
                )
                .limit(1)
            )
            workitem_closed = control is not None
    return mutation_fenced(dispatch, cancel_requested, workitem_closed)


async def authenticate(session: AsyncSession, dispatch_id: int, token: str | None) -> UploadAuth:
    """按调度找到执行器，再校验上传令牌。"""
    dispatch = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    executor = None
    if dispatch is not None and dispatch.executor_id is not None:
        executor = await session.scalar(
            select(Executor)
            .where(Executor.id == dispatch.executor_id, Executor.is_deleted == 0)
            .limit(1)
        )
    return authenticate_loaded(dispatch, executor, token)
