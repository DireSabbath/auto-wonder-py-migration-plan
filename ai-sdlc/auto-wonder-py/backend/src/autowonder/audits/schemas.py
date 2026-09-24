"""审计日志查询结果。``detailJson`` 是 JSON 文本，``gmtCreate`` 是毫秒时间戳。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class AuditLogView(ApiModel):
    """一条审计日志。"""

    id: int | None
    actor_id: int | None
    actor_type: str | None
    actor_name: str | None
    module: str | None
    action: str | None
    target_type: str | None
    target_id: int | None
    detail_json: str | None
    gmt_create: datetime | None
