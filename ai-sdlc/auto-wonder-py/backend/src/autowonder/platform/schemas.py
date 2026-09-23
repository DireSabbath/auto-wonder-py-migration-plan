"""平台品牌接口的请求和响应。"""

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
