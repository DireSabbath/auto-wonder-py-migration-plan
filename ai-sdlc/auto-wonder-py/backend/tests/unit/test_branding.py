"""平台品牌的地址、主题、颜色和 Logo 规则。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.config import Settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app
from autowonder.platform.branding import (
    DEFAULT_DEPLOYMENT_VERSION,
    MAX_LOGO_SIZE,
    branding_view,
    clear_logo_cache,
    effective_public_base_url,
    load_cached_logo,
    logo_extension,
    logo_path,
    normalize_deployment_version,
    normalize_optional_url,
    normalize_public_base_url,
    normalize_runtime_version,
    require_persistent_logo_storage,
    validate_primary_color,
    validate_theme_key,
)
from autowonder.storage.objects import InMemoryObjectStorage, MapObjectStorage


def _settings() -> Settings:
    return Settings(
        public_base_url="http://localhost:7002/",
        deployment_version="x.x.x",
        recommended_runtime_version="0.2.163",
        community_edition=True,
    )


def test_branding_rules_match_java() -> None:
    """域名、主题、颜色、版本和 Logo 类型按 Java 的校验结果收口。"""
    assert normalize_public_base_url("http://localhost:7002/") == "http://localhost:7002"
    try:
        normalize_public_base_url("http://localhost:7002/?q=1")
    except RuntimeError as error:
        assert "absolute http(s) URL" in str(error)
    else:
        raise AssertionError("expected public base url with query to fail")
    assert normalize_optional_url(" https://example.com/ ") == "https://example.com"
    assert normalize_optional_url("  ") is None
    try:
        normalize_optional_url("http://example.com")
    except BizError as error:
        assert str(error) == "域名格式不合法"
    else:
        raise AssertionError("expected http domain to fail")
    assert validate_theme_key(" teal ") == "teal"
    try:
        validate_theme_key("purple")
    except BizError as error:
        assert str(error) == "主题配色不合法"
    else:
        raise AssertionError("expected unknown theme")
    assert validate_primary_color("#F97316") == "#f97316"
    assert normalize_runtime_version("0.2.163") == "0.2.163"
    assert normalize_deployment_version("") == DEFAULT_DEPLOYMENT_VERSION
    assert normalize_deployment_version("1.2.3-rc.1") == "1.2.3-rc.1"
    try:
        normalize_deployment_version("latest")
    except RuntimeError as error:
        assert "semantic version" in str(error)
    else:
        raise AssertionError("expected invalid deployment version")
    assert logo_extension("image/jpeg; charset=binary", 12, False) == ".jpg"
    try:
        logo_extension("image/gif", 12, False)
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected unsupported logo type")
    try:
        logo_extension("image/png", MAX_LOGO_SIZE + 1, False)
    except BizError as error:
        assert str(error) == "Logo 文件不能超过 2MB"
    else:
        raise AssertionError("expected oversized logo")
    assert logo_path(None, 3) == "/logo.png"
    assert logo_path("bucket/logo.png", None) == "/api/platform/branding/logo?v=0"
    assert effective_public_base_url(" https://brand.example ", "http://localhost:7002") == (
        "https://brand.example"
    )
    view = branding_view(
        "  ",
        None,
        4,
        "ocean-blue",
        "#f97316",
        None,
        False,
        _settings(),
    )
    assert view.platform_name == "AutoWonder"
    assert view.mcp_base_url == "http://localhost:7002/api/mcp"
    assert view.community_edition is True
    assert view.can_manage is False
    assert view.logo_url == "/logo.png"
    try:
        require_persistent_logo_storage(InMemoryObjectStorage())
    except BizError as error:
        assert error.error_code == ErrorCode.STORAGE_ERROR
        assert str(error) == "Logo 上传失败"
    else:
        raise AssertionError("expected in-memory logo storage to fail")
    durable = MapObjectStorage()
    assert require_persistent_logo_storage(durable) is durable


def test_logo_cache_reuses_bytes_until_ttl() -> None:
    """同一引用在 5 分钟内只读一次对象存储。"""
    clear_logo_cache()
    storage = MapObjectStorage()
    storage.put("bucket", "logo.png", b"png-bytes")
    first = load_cached_logo("bucket/logo.png", "image/png", storage, 1_000)
    second = load_cached_logo("bucket/logo.png", "image/png", storage, 2_000)
    assert first == (b"png-bytes", "image/png")
    assert second == first
    assert storage.gets == ["bucket/logo.png"]
    clear_logo_cache()
    assert load_cached_logo("  ", None, storage, 3_000) is None


def test_branding_routes_keep_public_pages_open() -> None:
    """公开配置和 Logo 在白名单里；管理读取未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/platform/branding/public" in paths
    assert "/api/platform/branding/logo" in paths
    assert "/api/platform/branding" in paths
    assert "put" in paths["/api/platform/branding"]
    response = client.get("/api/platform/branding")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
