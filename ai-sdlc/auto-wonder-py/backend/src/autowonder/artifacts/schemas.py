"""产物查询结果。字段与 ``ArtifactVO`` 一致。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class ArtifactView(ApiModel):
    """一条用户可见产物。"""

    id: int | None = None
    workitem_id: int | None = None
    dispatch_id: int | None = None
    name: str | None = None
    type: str | None = None
    size: int | None = None
    gmt_create: datetime | None = None
