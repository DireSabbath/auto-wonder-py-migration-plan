"""把数字员工指派交给调度。入队使用调用方会话，拉起发生在提交之后。"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.dispatch.enqueue import enqueue_assignment
from autowonder.dispatch.models import Dispatch
from autowonder.dispatch.pending import drive_dispatch
from autowonder.workitems.events import WorkitemAssigned

logger = logging.getLogger(__name__)


async def on_workitem_assigned(
    session: AsyncSession, event: WorkitemAssigned
) -> Dispatch | None:
    """步骤或员工缺失时跳过。两者都在时按指派版本幂等入队。"""
    step_id = event.sdlc_step_id
    agent_id = event.agent_id
    if step_id is None or agent_id is None:
        logger.info(
            "workitem assigned skipped workitemId=%s (no sdlcStep or agent)",
            event.workitem_id,
        )
        return None
    logger.info(
        "workitem assigned workitemId=%s agentId=%s sdlcStepId=%s",
        event.workitem_id,
        agent_id,
        step_id,
    )
    return await enqueue_assignment(
        session,
        event.tenant_id,
        event.workitem_id,
        step_id,
        agent_id,
        event.assignment_version,
        event.user_id,
    )


async def drive_queued(dispatch: Dispatch | None) -> None:
    """提交之后再拉起刚入队的指派。没有新行时什么也不做。"""
    if dispatch is None or dispatch.id is None:
        return
    await drive_dispatch(dispatch.id)
