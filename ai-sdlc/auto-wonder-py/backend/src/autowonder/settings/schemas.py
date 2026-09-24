"""系统设置的请求和响应。secret 是布尔字段，JSON 名与 Java 一致。"""

from autowonder.core.schema import ApiModel


class SettingItem(ApiModel):
    """一条待写入的设置。省略 secret 时按非密文处理。"""

    key: str | None = None
    value_json: str | None = None
    secret: bool = False


class UpdateSettingsRequest(ApiModel):
    """按分组批量写入。items 为空时不改数据。"""

    items: list[SettingItem] | None = None


class SettingView(ApiModel):
    """一条设置。密文只返回掩码。"""

    id: int | None = None
    group: str | None = None
    key: str | None = None
    value_json: str | None = None
    secret: bool = False
