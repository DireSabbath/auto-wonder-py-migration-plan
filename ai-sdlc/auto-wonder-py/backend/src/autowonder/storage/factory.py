"""按配置选择唯一的对象存储后端。OSS 与 S3 不能同时开启。"""

from autowonder.config import Settings, get_settings
from autowonder.storage.objects import ObjectStorage, has_text, memory_storage
from autowonder.storage.oss import AliyunOssObjectStorage
from autowonder.storage.s3 import S3ObjectStorage

_RETIRED_BUCKETS = frozenset(
    {
        "autowonder-task-pkg-daily-tmp",
        "autowonder-artifacts-daily-tmp",
    }
)
_EXCLUSIVE = "oss.enabled and s3.enabled are mutually exclusive; disable one storage backend"
_CACHED: ObjectStorage | None = None
_CACHED_KEY: tuple[object, ...] | None = None


def build_object_storage() -> ObjectStorage:
    """配置完整时返回云客户端。OSS 开启但缺字段时按 Java 拒绝启动该客户端。"""
    global _CACHED, _CACHED_KEY
    settings = get_settings()
    key = _config_key(settings)
    if _CACHED is not None and _CACHED_KEY == key:
        return _CACHED
    client = _create(settings)
    _CACHED = client
    _CACHED_KEY = key
    return client


def resolve_bucket(workload: str, fallback: str) -> str:
    """工作负载桶为空时回到默认桶。"""
    if has_text(workload):
        return workload
    return fallback


def resolve_public_endpoint(public_endpoint: str, endpoint: str) -> str:
    """公网端点为空时沿用服务端点。"""
    if has_text(public_endpoint):
        return public_endpoint
    return endpoint


def reject_retired_bucket(property_name: str, bucket: str | None) -> None:
    """已下线的临时桶不能继续配置。"""
    if bucket is not None and bucket.strip() in _RETIRED_BUCKETS:
        raise RuntimeError(
            property_name
            + " references retired OSS bucket "
            + bucket
            + "; use the corresponding *-tmp-new bucket"
        )


def _create(settings: Settings) -> ObjectStorage:
    if settings.s3_enabled and settings.oss_enabled:
        raise RuntimeError(_EXCLUSIVE)
    if settings.s3_enabled:
        _validate_s3(settings)
        public = resolve_public_endpoint(settings.s3_public_endpoint, settings.s3_endpoint)
        return S3ObjectStorage(
            settings.s3_endpoint,
            public,
            settings.s3_region,
            settings.s3_access_key_id,
            settings.s3_access_key_secret,
            settings.s3_force_path_style,
        )
    if settings.oss_enabled:
        _validate_oss(settings)
        public = resolve_public_endpoint(settings.oss_public_endpoint, settings.oss_endpoint)
        return AliyunOssObjectStorage(
            settings.oss_endpoint,
            public,
            settings.oss_access_key_id,
            settings.oss_access_key_secret,
        )
    return memory_storage()


def _validate_oss(settings: Settings) -> None:
    _require("oss.endpoint", settings.oss_endpoint, " is required")
    _require("oss.bucket", settings.oss_bucket, " is required")
    _require("oss.access-key-id", settings.oss_access_key_id, " is required")
    _require("oss.access-key-secret", settings.oss_access_key_secret, " is required")
    reject_retired_bucket("oss.task-pkg-bucket", settings.oss_task_pkg_bucket)
    reject_retired_bucket("oss.artifact-bucket", settings.oss_artifact_bucket)
    public = resolve_public_endpoint(settings.oss_public_endpoint, settings.oss_endpoint)
    if _internal(settings.oss_endpoint) and not has_text(settings.oss_public_endpoint):
        raise RuntimeError("oss.public-endpoint is required when oss.endpoint is internal")
    if _internal(public):
        raise RuntimeError("oss.public-endpoint must be externally reachable")


def _validate_s3(settings: Settings) -> None:
    suffix = " is required when s3.enabled is true"
    _require("s3.endpoint", settings.s3_endpoint, suffix)
    _require("s3.region", settings.s3_region, suffix)
    _require("s3.access-key-id", settings.s3_access_key_id, suffix)
    _require("s3.access-key-secret", settings.s3_access_key_secret, suffix)


def _require(property_name: str, value: str, suffix: str) -> None:
    if not has_text(value):
        raise RuntimeError(property_name + suffix)


def _internal(endpoint: str) -> bool:
    if not has_text(endpoint):
        return False
    return "-internal." in endpoint.lower()


def _config_key(settings: Settings) -> tuple[object, ...]:
    return (
        settings.s3_enabled,
        settings.oss_enabled,
        settings.s3_endpoint,
        settings.s3_public_endpoint,
        settings.s3_region,
        settings.s3_access_key_id,
        settings.s3_access_key_secret,
        settings.s3_force_path_style,
        settings.oss_endpoint,
        settings.oss_public_endpoint,
        settings.oss_bucket,
        settings.oss_access_key_id,
        settings.oss_access_key_secret,
        settings.oss_task_pkg_bucket,
        settings.oss_artifact_bucket,
    )
