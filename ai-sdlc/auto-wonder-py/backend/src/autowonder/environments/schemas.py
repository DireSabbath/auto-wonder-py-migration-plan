"""环境变量接口的请求和响应。列表里的值固定脱敏。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class CreateEnvironmentVariableRequest(ApiModel):
    """创建环境变量。value 必须显式出现。"""

    name: str | None = None
    value: str | None = None
    description: str | None = None


class UpdateEnvironmentVariableRequest(ApiModel):
    """更新环境变量。updateValue 用来区分只改说明和同时改值。"""

    name: str | None = None
    description: str | None = None
    update_value: bool | None = None
    value: str | None = None


class EnvironmentVariableView(ApiModel):
    """环境变量卡片。value 恒为 ``**``。"""

    id: int | None = None
    name: str | None = None
    value: str | None = None
    description: str | None = None
    gmt_create: datetime | None = None
    gmt_modified: datetime | None = None
    version: int | None = None


class EnvironmentVariableValueView(ApiModel):
    """明文只出现在单独的查看接口。"""

    value: str | None = None
