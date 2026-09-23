"""平台品牌接口的请求和响应。"""

from pydantic import Field

from autowonder.core.schema import ApiModel


class UpdateBrandingRequest(ApiModel):
    """更新平台名称、主题和域名。"""

    platform_name: str | None = None
    theme_key: str | None = None
    primary_color: str | None = None
    domain: str | None = None


class BrandingView(ApiModel):
    """对外品牌配置。布尔字段始终返回。"""

    platform_name: str
    logo_url: str
    theme_key: str
    primary_color: str
    domain: str | None = None
    mcp_base_url: str
    recommended_runtime_version: str
    deployment_version: str
    community_edition: bool
    can_manage: bool


class LogoUploadView(ApiModel):
    """上传成功后的 Logo 地址。"""

    logo_url: str


class PlatformAdminView(ApiModel):
    """名册里的一名平台管理员。不可移除时带上原因。"""

    user_id: int
    username: str
    nickname: str | None
    email: str | None
    active: bool
    subject: bool = Field(serialization_alias="self")
    removable: bool
    remove_disabled_reason: str | None


class PlatformAdminListView(ApiModel):
    """平台管理员名册。候选人由单独的搜索接口返回。"""

    admins: list[PlatformAdminView]
    can_manage: bool


class PlatformAdminCandidateView(ApiModel):
    """可提升为平台管理员的活跃用户。"""

    user_id: int
    username: str
    nickname: str | None
    email: str | None


class AddPlatformAdminRequest(ApiModel):
    """要提升的用户。缺省或 null 都表示没有指定用户。"""

    user_id: int | None = None
