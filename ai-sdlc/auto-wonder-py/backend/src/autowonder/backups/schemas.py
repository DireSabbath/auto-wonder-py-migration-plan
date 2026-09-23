"""备份历史。时间字段保持数据库读出的文本，不转成毫秒。"""

from autowonder.core.schema import ApiModel


class BackupView(ApiModel):
    """一条备份记录。"""

    id: str
    status: str
    oss_ref: str | None
    size_bytes: int | None
    sha256: str | None
    error_message: str | None
    created_at: str | None
    finished_at: str | None
