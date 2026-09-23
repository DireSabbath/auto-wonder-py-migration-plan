"""记忆接口的请求和响应。JSON 字段名与 Java VO 一致。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class CreateMemoryRequest(ApiModel):
    """手工创建记忆。标题必填，组织范围不保留 owner。"""

    scope: str | None = None
    owner_ref: int | None = None
    type: str | None = None
    title: str | None = None
    content_md: str | None = None


class UpdateMemoryRequest(ApiModel):
    """更新标题、正文、类型和范围。null 字段保持原值。"""

    title: str | None = None
    content_md: str | None = None
    type: str | None = None
    scope: str | None = None
    owner_ref: int | None = None


class ReviewRequest(ApiModel):
    """审核。decision 只能是 ADOPT 或 REJECT。"""

    decision: str | None = None
    edited_content_md: str | None = None
    edited_type: str | None = None
    comment: str | None = None
    scope: str | None = None
    owner_ref: int | None = None


class ImportFromArtifactRequest(ApiModel):
    """从产物导入一条待审核记忆。范围按请求原样保存。"""

    artifact_id: int | None = None
    scope: str | None = None
    owner_ref: int | None = None
    title: str | None = None
    content_md: str | None = None
    type: str | None = None


class MemoryView(ApiModel):
    """一条记忆。sourceRef 是 JSON 文本，创建响应不回读时间。"""

    id: int | None = None
    scope: str | None = None
    owner_ref: int | None = None
    type: str | None = None
    title: str | None = None
    content_md: str | None = None
    status: str | None = None
    source: str | None = None
    source_ref: str | None = None
    version: int | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None


class MemoryGroupView(ApiModel):
    """按范围和所有者聚合的一页记忆。"""

    scope: str | None = None
    owner_ref: int | None = None
    owner_name: str | None = None
    total: int | None = None
    memories: list[MemoryView] | None = None


class MemoryReviewView(ApiModel):
    """一次审核或范围变更记录。"""

    id: int | None = None
    memory_id: int | None = None
    reviewer_id: int | None = None
    decision: str | None = None
    edited_content_md: str | None = None
    comment: str | None = None
    gmt_create: datetime | None = None
