"""debug 日志查询结果。字段与 ``DebugLogVO`` 一致。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class DebugLogView(ApiModel):
    """一行 debug 日志登记。只有 UPLOADED 行带下载地址。"""

    id: int | None = None
    source_type: str | None = None
    source_id: int | None = None
    dispatch_id: int | None = None
    agent_id: int | None = None
    run_no: int | None = None
    dispatch_status: str | None = None
    object_key: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    truncated: bool | None = None
    upload_channel: str | None = None
    status: str | None = None
    error_message: str | None = None
    gmt_create: datetime | None = None
    download_url: str | None = None
