"""调度查询结果。列表不含产物，详情才带。"""

from datetime import datetime

from autowonder.artifacts.schemas import ArtifactView
from autowonder.core.schema import ApiModel


class DispatchView(ApiModel):
    """一条调度记录及展示用名称。"""

    id: int | None = None
    source_type: str | None = None
    workitem_id: int | None = None
    sdlc_step_id: int | None = None
    agent_id: int | None = None
    agent_version_id: int | None = None
    executor_id: int | None = None
    status: str | None = None
    attempt: int | None = None
    result_summary: str | None = None
    error: str | None = None
    package_oss_ref: str | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None
    workitem_title: str | None = None
    agent_name: str | None = None
    agent_version_no: int | None = None
    executor_name: str | None = None
    artifacts: list[ArtifactView] | None = None


class DispatchPage(ApiModel):
    """调度分页。页码字段是 page 和 pageSize。"""

    list: list[DispatchView]
    total: int
    page: int
    page_size: int
