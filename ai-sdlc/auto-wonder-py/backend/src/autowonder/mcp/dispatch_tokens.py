"""调度 MCP 令牌。24 小时，只在派发仍活跃且未被围栏挡住时有效。"""

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.recovery import execution_source, fenced
from autowonder.mcp.principal import CredentialType, Principal
from autowonder.scheduledtasks.models import ScheduledTaskRun
from autowonder.security.jwt import parse_scoped, sign_scoped
from autowonder.workitems.models import Workitem
from autowonder.workspaces.models import OrgMember

PREFIX = "awdispatch_"
_PURPOSE = "dispatch-mcp"
_TTL_SECONDS = 24 * 60 * 60
_ACTIVE = frozenset({"PACKAGING", "PENDING", "DISPATCHED", "ACKED", "RUNNING", "PAUSING"})


async def issue_dispatch_token(session: AsyncSession, dispatch: Dispatch) -> str:
    """按运行所有者、工单创建人或指派人确定主体后签发。"""
    user_id = 0 if dispatch.creator_id is None else dispatch.creator_id
    if execution_source(dispatch) == "SCHEDULED_TASK_RUN":
        run = await session.scalar(
            select(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.workspace_id == dispatch.tenant_id,
                ScheduledTaskRun.id == dispatch.workitem_id,
            )
            .limit(1)
        )
        if (
            run is not None
            and dispatch.tenant_id == run.workspace_id
            and run.owner_id is not None
            and run.owner_id > 0
        ):
            user_id = run.owner_id
    if user_id <= 0:
        workitem = await session.scalar(
            select(Workitem)
            .where(Workitem.id == dispatch.workitem_id, Workitem.is_deleted == 0)
            .limit(1)
        )
        if workitem is not None and dispatch.tenant_id == workitem.tenant_id:
            user_id = _positive(workitem.creator_id, workitem.assign_operator_id, user_id)
    if user_id <= 0:
        raise RuntimeError(f"dispatch MCP principal is unavailable for dispatch {dispatch.id}")
    signed = sign_scoped(user_id, dispatch.tenant_id, _PURPOSE, dispatch.id, _TTL_SECONDS)
    return PREFIX + signed


async def authenticate_dispatch(session: AsyncSession, token: str | None) -> Principal:
    """前缀、用途、活跃状态或围栏任一不满足都是未授权。"""
    try:
        return await _principal(session, token)
    except Exception as error:
        raise _unauthorized() from error


async def _principal(session: AsyncSession, token: str | None) -> Principal:
    if token is None or not token.startswith(PREFIX):
        raise ValueError("invalid prefix")
    claims = parse_scoped(token[len(PREFIX) :])
    if claims["purpose"] != _PURPOSE:
        raise ValueError("invalid purpose")
    dispatch_id = cast(int, claims["subjectId"])
    workspace_id = cast(int, claims["workspace"])
    user_id = cast(int, claims["uid"])
    dispatch = await session.scalar(
        select(Dispatch).where(Dispatch.id == dispatch_id, Dispatch.is_deleted == 0).limit(1)
    )
    if (
        dispatch is None
        or dispatch.tenant_id != workspace_id
        or dispatch.status not in _ACTIVE
        or await fenced(session, dispatch)
    ):
        raise ValueError("dispatch is inactive")
    level = await _access_level(session, workspace_id, user_id)
    return Principal(workspace_id, user_id, -dispatch_id, level, CredentialType.DISPATCH)


async def _access_level(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
) -> WorkspaceAccessLevel:
    member = await session.scalar(
        select(OrgMember)
        .where(
            OrgMember.tenant_id == workspace_id,
            OrgMember.user_id == user_id,
            OrgMember.is_deleted == 0,
        )
        .limit(1)
    )
    if member is None or member.access_level is None:
        return WorkspaceAccessLevel.READ_WRITE
    try:
        return WorkspaceAccessLevel[member.access_level]
    except KeyError:
        return WorkspaceAccessLevel.READ_WRITE


def _positive(creator_id: int | None, assign_operator_id: int | None, current: int) -> int:
    if creator_id is not None and creator_id > 0:
        return creator_id
    if assign_operator_id is not None and assign_operator_id > 0:
        return assign_operator_id
    return current


def _unauthorized() -> BizError:
    return BizError(ErrorCode.UNAUTHORIZED)
