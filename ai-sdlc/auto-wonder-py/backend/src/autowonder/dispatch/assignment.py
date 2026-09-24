"""把数字员工指派交给调度。入队和立即执行随调度主环迁移。"""

import logging

from autowonder.workitems.events import WorkitemAssigned

logger = logging.getLogger(__name__)


def on_workitem_assigned(event: WorkitemAssigned) -> None:
    """步骤或员工缺失时跳过。两者都在时记录指派，尚不创建调度行。"""
    if event.sdlc_step_id is None or event.agent_id is None:
        logger.info(
            "workitem assigned skipped workitemId=%s (no sdlcStep or agent)",
            event.workitem_id,
        )
        return
    logger.info(
        "workitem assigned workitemId=%s agentId=%s sdlcStepId=%s",
        event.workitem_id,
        event.agent_id,
        event.sdlc_step_id,
    )
