"""工单澄清材料。没有记录时仍返回工单 id 和版本 0。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class PutClarificationRequest(ApiModel):
    """整篇替换澄清正文。"""

    content_md: str | None = None


class ClarificationView(ApiModel):
    """澄清材料。"""

    workitem_id: int | None = None
    content_md: str | None = None
    version: int | None = None
    gmt_modified: datetime | None = None


def empty_clarification(workitem_id: int) -> ClarificationView:
    """还没有澄清记录时的占位结果。"""
    return ClarificationView(
        workitem_id=workitem_id,
        content_md=None,
        version=0,
        gmt_modified=None,
    )
