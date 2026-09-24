"""通知列表和偏好。分页字段是 items 与 total。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class NotificationView(ApiModel):
    """一条站内通知。"""

    id: int | None = None
    type: str | None = None
    title: str | None = None
    content: str | None = None
    link: str | None = None
    ref_type: str | None = None
    ref_id: int | None = None
    status: str | None = None
    gmt_create: datetime | None = None


class NotificationPage(ApiModel):
    """通知列表。"""

    items: list[NotificationView]
    total: int


class NotifyPrefView(ApiModel):
    """一种通知的渠道开关。未选中的 IM 渠道固定为关。"""

    type: str | None = None
    in_app: bool = False
    dingtalk: bool = False
    feishu: bool = False


class PrefItem(ApiModel):
    """更新一条偏好。省略的布尔值按关处理。"""

    type: str | None = None
    in_app: bool = False
    dingtalk: bool = False
    feishu: bool = False


class UpdatePrefRequest(ApiModel):
    """批量更新偏好。空列表不写库。"""

    items: list[PrefItem] | None = None
