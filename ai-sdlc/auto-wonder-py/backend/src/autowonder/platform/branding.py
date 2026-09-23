"""平台品牌。域名优先于部署地址；Logo 写入制品桶。"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import Settings, get_settings
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.platform.models import PlatformBrandingConfig
from autowonder.platform.schemas import BrandingView, LogoUploadView, UpdateBrandingRequest
from autowonder.platform.service import is_system_admin, require_system_admin
from autowonder.storage.objects import InMemoryObjectStorage, ObjectStorage, get_object_storage

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM_NAME = "AutoWonder"
DEFAULT_THEME_KEY = "aliyun-orange"
DEFAULT_PRIMARY_COLOR = "#f97316"
DEFAULT_DEPLOYMENT_VERSION = "x.x.x"
_THEME_KEYS = {
    "aliyun-orange",
    "ocean-blue",
    "jade-green",
    "indigo",
    "rose",
    "cyan",
    "amber",
    "violet",
    "graphite",
    "teal",
}
_LOGO_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
MAX_LOGO_SIZE = 2 * 1024 * 1024
_LOGO_CACHE_TTL_MS = 5 * 60 * 1000
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_PUBLIC_BASE_ERROR = "autowonder.public-base-url must be an absolute http(s) URL"


@dataclass
class _LogoCache:
    oss_ref: str
    data: bytes
    content_type: str
    expires_at_ms: int


_logo_cache: _LogoCache | None = None


def clear_logo_cache() -> None:
    """上传新 Logo 后丢掉进程内缓存。"""
    global _logo_cache
    _logo_cache = None


def java_utf16_length(text: str) -> int:
    """Java ``String.length()`` 计的是 UTF-16 代码单元。"""
    return len(text.encode("utf-16-le")) // 2


def normalize_public_base_url(value: str | None) -> str:
    """部署根地址必须是不带查询和片段的 http(s) URL。"""
    trimmed = ""
    if value is not None:
        trimmed = value.strip()
    if trimmed == "":
        raise RuntimeError("autowonder.public-base-url must be configured")
    parsed = urlsplit(trimmed)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or parsed.hostname is None:
        raise RuntimeError(_PUBLIC_BASE_ERROR)
    if parsed.query != "" or parsed.fragment != "":
        raise RuntimeError(_PUBLIC_BASE_ERROR)
    return re.sub(r"/+$", "", trimmed)


def normalize_runtime_version(value: str | None) -> str:
    """推荐执行器版本必须是语义化版本。"""
    trimmed = ""
    if value is not None:
        trimmed = value.strip()
    if _SEMVER.fullmatch(trimmed) is None:
        raise RuntimeError("autowonder.runtime.recommended-version must be a semantic version")
    return trimmed


def normalize_deployment_version(value: str | None) -> str:
    """部署版本允许占位 ``x.x.x``，其余必须是语义化版本。"""
    trimmed = ""
    if value is not None:
        trimmed = value.strip()
    if trimmed == "":
        return DEFAULT_DEPLOYMENT_VERSION
    if trimmed == DEFAULT_DEPLOYMENT_VERSION:
        return trimmed
    if _SEMVER.fullmatch(trimmed) is not None:
        return trimmed
    raise RuntimeError("autowonder.version must be x.x.x or a semantic version")


def normalize_optional_url(value: str | None) -> str | None:
    """私有化域名可空；有值时必须是 https URL。"""
    trimmed = ""
    if value is not None:
        trimmed = value.strip()
    if trimmed == "":
        return None
    parsed = urlsplit(trimmed)
    if parsed.hostname is None or parsed.scheme.lower() != "https":
        raise BizError(ErrorCode.PARAM_INVALID, "域名格式不合法")
    return re.sub(r"/+$", "", trimmed)


def non_blank(value: str | None, message: str, max_length: int) -> str:
    """去掉首尾空白后不能为空，也不能超过列宽。"""
    trimmed = ""
    if value is not None:
        trimmed = value.strip()
    if trimmed == "" or java_utf16_length(trimmed) > max_length:
        raise BizError(ErrorCode.PARAM_INVALID, message)
    return trimmed


def validate_theme_key(value: str | None) -> str:
    """只接受页面上的十个主题。"""
    trimmed = ""
    if value is not None:
        trimmed = value.strip()
    if trimmed not in _THEME_KEYS:
        raise BizError(ErrorCode.PARAM_INVALID, "主题配色不合法")
    return trimmed


def validate_primary_color(value: str | None) -> str:
    """主题色是 ``#`` 加 6 位十六进制，入库时转成小写。"""
    trimmed = ""
    if value is not None:
        trimmed = value.strip()
    if _COLOR.fullmatch(trimmed) is None:
        raise BizError(ErrorCode.PARAM_INVALID, "主题颜色格式不合法")
    return trimmed.lower()


def normalize_content_type(value: str | None) -> str:
    """去掉 MIME 参数并转成小写。"""
    if value is None:
        return ""
    semicolon = value.find(";")
    head = value
    if semicolon >= 0:
        head = value[:semicolon]
    return head.strip().lower()


def logo_extension(content_type: str | None, size: int, empty: bool) -> str:
    """空文件、超过 2MB 和非图片类型都是参数错误。"""
    if empty:
        raise BizError(ErrorCode.PARAM_INVALID, "Logo 文件不能为空")
    if size > MAX_LOGO_SIZE:
        raise BizError(ErrorCode.PARAM_INVALID, "Logo 文件不能超过 2MB")
    extension = _LOGO_EXTENSIONS.get(normalize_content_type(content_type))
    if extension is None:
        raise BizError(ErrorCode.PARAM_INVALID, "Logo 仅支持 PNG、JPG、WebP")
    return extension


def safe_text(value: str | None, fallback: str) -> str:
    """空白配置回退到默认值，非空白原样返回。"""
    if value is None:
        return fallback
    if value.strip() == "":
        return fallback
    return value


def configured_domain(value: str | None) -> bool:
    """库里的域名有非空白内容时，覆盖部署根地址。"""
    if value is None:
        return False
    return value.strip() != ""


def effective_public_base_url(domain: str | None, public_base_url: str) -> str:
    """已保存的域名优先，否则用部署配置的根地址。"""
    if configured_domain(domain) and domain is not None:
        return domain.strip()
    return public_base_url


def logo_path(logo_oss_ref: str | None, version: int | None) -> str:
    """没有 Logo 时用前端静态图，有 Logo 时带上版本避免缓存旧图。"""
    if logo_oss_ref is None or logo_oss_ref.strip() == "":
        return "/logo.png"
    shown = 0
    if version is not None:
        shown = version
    return "/api/platform/branding/logo?v=" + str(shown)


def require_persistent_logo_storage(storage: ObjectStorage) -> ObjectStorage:
    """Logo 必须进可持久化的对象存储。进程内存储不能冒充上传成功。"""
    if isinstance(storage, InMemoryObjectStorage):
        raise BizError(ErrorCode.STORAGE_ERROR, "Logo 上传失败")
    return storage


def artifact_bucket(workload_bucket: str, base_bucket: str) -> str:
    """制品桶未单独配置时回退到默认桶。"""
    if workload_bucket.strip() != "":
        return workload_bucket
    return base_bucket


def branding_view(
    platform_name: str | None,
    logo_oss_ref: str | None,
    version: int | None,
    theme_key: str | None,
    primary_color: str | None,
    domain: str | None,
    can_manage: bool,
    settings: Settings,
) -> BrandingView:
    """组装品牌响应。根地址和版本在这里按启动配置校验。"""
    public_base_url = normalize_public_base_url(settings.public_base_url)
    base_url = effective_public_base_url(domain, public_base_url)
    return BrandingView(
        platform_name=safe_text(platform_name, DEFAULT_PLATFORM_NAME),
        logo_url=logo_path(logo_oss_ref, version),
        theme_key=safe_text(theme_key, DEFAULT_THEME_KEY),
        primary_color=safe_text(primary_color, DEFAULT_PRIMARY_COLOR),
        domain=domain,
        mcp_base_url=base_url + "/api/mcp",
        recommended_runtime_version=normalize_runtime_version(
            settings.recommended_runtime_version
        ),
        deployment_version=normalize_deployment_version(settings.deployment_version),
        community_edition=settings.community_edition,
        can_manage=can_manage,
    )


def load_cached_logo(
    oss_ref: str | None,
    content_type: str | None,
    storage: ObjectStorage,
    now_ms: int,
) -> tuple[bytes, str] | None:
    """按引用读取 Logo，并在 5 分钟内复用进程内缓存。"""
    global _logo_cache
    if oss_ref is None or oss_ref.strip() == "":
        return None
    cached = _logo_cache
    if cached is not None and cached.oss_ref == oss_ref and cached.expires_at_ms > now_ms:
        return cached.data, cached.content_type
    data = storage.get(oss_ref)
    if data is None:
        return None
    shown_type = "application/octet-stream"
    if content_type is not None and content_type.strip() != "":
        shown_type = content_type
    _logo_cache = _LogoCache(oss_ref, data, shown_type, now_ms + _LOGO_CACHE_TTL_MS)
    return data, shown_type


async def public_branding(session: AsyncSession) -> BrandingView:
    """登录前可见的品牌配置，不带管理权限。"""
    current = await _current(session)
    return _view(current, False)


async def admin_branding(session: AsyncSession, user_id: int | None) -> BrandingView:
    """登录后的品牌配置。能否管理只看平台管理员标志。"""
    current = await _current(session)
    can_manage = False
    if user_id is not None:
        can_manage = await is_system_admin(session, user_id)
    return _view(current, can_manage)


async def update_branding(
    session: AsyncSession,
    user_id: int | None,
    request: UpdateBrandingRequest,
) -> BrandingView:
    """平台管理员更新名称、主题、颜色和域名。"""
    await require_system_admin(session, user_id, "修改平台配置")
    platform_name = non_blank(request.platform_name, "平台名称不能为空", 128)
    theme_key = validate_theme_key(request.theme_key)
    primary_color = validate_primary_color(request.primary_color)
    domain = normalize_optional_url(request.domain)
    result = await session.execute(
        update(PlatformBrandingConfig)
        .where(PlatformBrandingConfig.id == 1, PlatformBrandingConfig.is_deleted == 0)
        .values(
            platform_name=platform_name,
            theme_key=theme_key,
            primary_color=primary_color,
            domain=domain,
            modifier_id=user_id,
            version=PlatformBrandingConfig.version + 1,
            gmt_modified=now_local(),
        )
    )
    if cast(CursorResult[Any], result).rowcount == 0:
        raise BizError(ErrorCode.NOT_FOUND)
    await session.commit()
    return _view(await _current(session), True)


async def upload_logo(
    session: AsyncSession,
    user_id: int | None,
    content_type: str | None,
    data: bytes,
    storage: ObjectStorage | None = None,
) -> LogoUploadView:
    """校验图片后写入制品桶，并让 Logo 地址带上新版本。"""
    await require_system_admin(session, user_id, "上传平台 Logo")
    extension = logo_extension(content_type, len(data), len(data) == 0)
    target = storage
    if target is None:
        target = get_object_storage()
    target = require_persistent_logo_storage(target)
    settings = get_settings()
    bucket = artifact_bucket(settings.oss_artifact_bucket, settings.oss_bucket)
    key = "platform/branding/logo-" + str(int(time.time() * 1000)) + extension
    try:
        stored = target.put(bucket, key, data)
        result = await session.execute(
            update(PlatformBrandingConfig)
            .where(PlatformBrandingConfig.id == 1, PlatformBrandingConfig.is_deleted == 0)
            .values(
                logo_oss_ref=stored.oss_ref,
                logo_content_type=normalize_content_type(content_type),
                modifier_id=user_id,
                version=PlatformBrandingConfig.version + 1,
                gmt_modified=now_local(),
            )
        )
        if cast(CursorResult[Any], result).rowcount == 0:
            raise BizError(ErrorCode.NOT_FOUND)
        await session.commit()
    except BizError:
        raise
    except Exception as error:
        raise BizError(ErrorCode.STORAGE_ERROR, "Logo 上传失败") from error
    clear_logo_cache()
    current = await _current(session)
    return LogoUploadView(logo_url=logo_path(current.logo_oss_ref, current.version))


async def read_logo(
    session: AsyncSession,
    storage: ObjectStorage | None = None,
) -> tuple[bytes, str] | None:
    """读取当前 Logo。没有引用或对象不存在时返回空。"""
    current = await _current(session)
    target = storage
    if target is None:
        target = get_object_storage()
    return load_cached_logo(
        current.logo_oss_ref,
        current.logo_content_type,
        target,
        int(time.time() * 1000),
    )


async def _current(session: AsyncSession) -> PlatformBrandingConfig:
    row = await session.scalar(
        select(PlatformBrandingConfig)
        .where(PlatformBrandingConfig.id == 1, PlatformBrandingConfig.is_deleted == 0)
        .limit(1)
    )
    if row is not None:
        return row
    return PlatformBrandingConfig(
        platform_name=DEFAULT_PLATFORM_NAME,
        theme_key=DEFAULT_THEME_KEY,
        primary_color=DEFAULT_PRIMARY_COLOR,
        domain=None,
    )


def _view(current: PlatformBrandingConfig, can_manage: bool) -> BrandingView:
    return branding_view(
        current.platform_name,
        current.logo_oss_ref,
        current.version,
        current.theme_key,
        current.primary_color,
        current.domain,
        can_manage,
        get_settings(),
    )
