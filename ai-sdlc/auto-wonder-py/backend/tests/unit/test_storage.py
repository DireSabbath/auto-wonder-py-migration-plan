"""对象存储的引用、后端选择和预签名。这些检查不访问真实的桶。"""

from urllib.parse import urlsplit

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from autowonder.config import Settings
from autowonder.storage.factory import (
    build_object_storage,
    reject_retired_bucket,
    resolve_bucket,
    resolve_public_endpoint,
)
from autowonder.storage.objects import (
    InMemoryObjectStorage,
    ObjectStorageError,
    get_object_storage,
    md5_hex,
    split_oss_ref,
)
from autowonder.storage.oss import AliyunOssObjectStorage
from autowonder.storage.s3 import S3ObjectStorage


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _S3Service:
    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.deleted = False
        self.payload = b"\x01\x02\x03"

    def put_object(self, **_kwargs: object) -> None:
        return None

    def get_object(self, **_kwargs: object) -> dict[str, _Body]:
        if self.error is not None:
            raise self.error
        return {"Body": _Body(self.payload)}

    def head_object(self, **_kwargs: object) -> dict[str, str]:
        if self.error is not None:
            raise self.error
        return {}

    def delete_object(self, **_kwargs: object) -> None:
        if self.error is not None:
            raise self.error
        self.deleted = True


class _Signer:
    def generate_presigned_url(self, *_args: object, **_kwargs: object) -> str:
        return "https://signed.example/object"


class _OssBucket:
    def __init__(self) -> None:
        self.puts = 0
        self.error: BaseException | None = None
        self.payload = b"\x0a\x14\x1e"
        self.signed = "https://bucket.oss-cn-shanghai.aliyuncs.com/object?signature=test"

    def put_object(self, _key: str, _data: bytes) -> None:
        self.puts = self.puts + 1

    def get_object(self, _key: str) -> _Body:
        if self.error is not None:
            raise self.error
        return _Body(self.payload)

    def sign_url(self, method: str, key: str, _expires: int) -> str:
        return self.signed + "&method=" + method + "&key=" + key

    def delete_object(self, _key: str) -> None:
        if self.error is not None:
            raise self.error

    def object_exists(self, _key: str) -> bool:
        if self.error is not None:
            raise self.error
        return True


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


def _use(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr("autowonder.storage.factory.get_settings", lambda: settings)


def test_refs_match_the_java_vectors() -> None:
    """MD5 与按第一个斜杠拆分的结果和 Java 单测一致。"""
    assert md5_hex(b"hello") == "5d41402abc4b2a76b9719d911017c592"
    assert split_oss_ref("bkt/a/b/c.zip") == ("bkt", "a/b/c.zip")
    assert split_oss_ref("b/k") == ("b", "k")
    with pytest.raises(ValueError, match="bad ossRef: null"):
        split_oss_ref(None)
    with pytest.raises(ValueError, match="bad ossRef: bucketonly"):
        split_oss_ref("bucketonly")
    with pytest.raises(ValueError, match="bad ossRef: /key"):
        split_oss_ref("/key")
    with pytest.raises(ValueError, match="bad ossRef: bucket/"):
        split_oss_ref("bucket/")


def test_memory_roundtrip_and_presign_put() -> None:
    """进程内存储计算 MD5，上传地址带秒数。"""
    storage = InMemoryObjectStorage()
    stored = storage.put("bkt", "a/b.zip", b"hello")
    assert stored.oss_ref == "bkt/a/b.zip"
    assert stored.size == 5
    assert stored.md5 == "5d41402abc4b2a76b9719d911017c592"
    assert storage.get(stored.oss_ref) == b"hello"
    assert "bkt/k" in storage.presign_get("bkt/k", 600)
    assert storage.presign_put("bucket", "debug/1/k.log.gz", 1200) == (
        "mem-put://bucket/debug/1/k.log.gz?ttl=1200"
    )
    assert storage.get("nope/x") is None
    storage.delete("nope/x")
    storage.delete(stored.oss_ref)
    assert storage.get(stored.oss_ref) is None


def test_storage_error_describe_and_permanent_codes() -> None:
    """配置类错误码与描述文本和 Java 一致。"""
    error = ObjectStorageError("s3 get failed", "bucket", "key", "AccessDenied")
    assert error.is_permanent_configuration_error() is True
    assert error.describe() == "AccessDenied bucket=bucket key=key"
    blank = ObjectStorageError("s3 get failed", " ", None, None)
    assert blank.is_permanent_configuration_error() is False
    assert blank.describe() == "ObjectStorageError"


def test_default_oss_config_requires_endpoint() -> None:
    """默认开启 OSS 且端点为空时，拒绝创建客户端。"""
    with pytest.raises(RuntimeError) as error:
        get_object_storage()
    assert str(error.value) == "oss.endpoint is required"


def test_backend_selection_matches_java(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个后端互斥。都关闭时才使用进程内存储。"""
    _use(monkeypatch, Settings(OSS_ENABLED=False, S3_ENABLED=False))
    assert isinstance(build_object_storage(), InMemoryObjectStorage)
    _use(
        monkeypatch,
        Settings(
            OSS_ENABLED=True,
            S3_ENABLED=True,
            S3_ENDPOINT="http://minio.example.com:9000",
            S3_ACCESS_KEY_ID="id",
            S3_ACCESS_KEY_SECRET="secret",
        ),
    )
    with pytest.raises(RuntimeError) as exclusive:
        build_object_storage()
    assert str(exclusive.value) == (
        "oss.enabled and s3.enabled are mutually exclusive; disable one storage backend"
    )
    _use(
        monkeypatch,
        Settings(
            OSS_ENABLED=False,
            S3_ENABLED=True,
            S3_ENDPOINT="http://minio-internal.example.com:9000",
            S3_PUBLIC_ENDPOINT="http://minio.example.com:9000",
            S3_REGION="us-east-1",
            S3_ACCESS_KEY_ID="test-access-key-id",
            S3_ACCESS_KEY_SECRET="test-access-key-secret",
        ),
    )
    selected = build_object_storage()
    assert isinstance(selected, S3ObjectStorage)
    _use(monkeypatch, Settings(OSS_ENABLED=False, S3_ENABLED=True))
    with pytest.raises(RuntimeError) as missing:
        build_object_storage()
    assert str(missing.value) == "s3.endpoint is required when s3.enabled is true"


def test_oss_validation_retired_buckets_and_internal_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下线桶和内网端点在创建客户端之前拒绝。"""
    assert resolve_bucket("", "community-bucket") == "community-bucket"
    assert resolve_bucket("skill-bucket", "community-bucket") == "skill-bucket"
    assert resolve_public_endpoint("", "https://oss-cn-shanghai.aliyuncs.com") == (
        "https://oss-cn-shanghai.aliyuncs.com"
    )
    assert (
        resolve_public_endpoint(
            "https://oss-accelerate.aliyuncs.com",
            "https://oss-cn-shanghai.aliyuncs.com",
        )
        == "https://oss-accelerate.aliyuncs.com"
    )
    with pytest.raises(RuntimeError) as retired:
        reject_retired_bucket("oss.artifact-bucket", "autowonder-artifacts-daily-tmp")
    assert "retired OSS bucket" in str(retired.value)
    _use(
        monkeypatch,
        Settings(
            OSS_ENABLED=True,
            S3_ENABLED=False,
            OSS_ENDPOINT="https://oss-cn-shanghai-internal.aliyuncs.com",
            OSS_BUCKET="community-bucket",
            OSS_ACCESS_KEY_ID="id",
            OSS_ACCESS_KEY_SECRET="secret",
        ),
    )
    with pytest.raises(RuntimeError) as internal:
        build_object_storage()
    assert str(internal.value) == ("oss.public-endpoint is required when oss.endpoint is internal")
    _use(
        monkeypatch,
        Settings(
            OSS_ENABLED=True,
            S3_ENABLED=False,
            OSS_ENDPOINT="https://oss-cn-shanghai-internal.aliyuncs.com",
            OSS_PUBLIC_ENDPOINT="https://oss-cn-shanghai-internal.aliyuncs.com",
            OSS_BUCKET="community-bucket",
            OSS_ACCESS_KEY_ID="id",
            OSS_ACCESS_KEY_SECRET="secret",
        ),
    )
    with pytest.raises(RuntimeError) as public:
        build_object_storage()
    assert str(public.value) == "oss.public-endpoint must be externally reachable"
    _use(
        monkeypatch,
        Settings(
            OSS_ENABLED=True,
            S3_ENABLED=False,
            OSS_ENDPOINT="https://oss-cn-shanghai.aliyuncs.com",
            OSS_BUCKET="community-bucket",
            OSS_ACCESS_KEY_ID="id",
            OSS_ACCESS_KEY_SECRET="secret",
            OSS_ARTIFACT_BUCKET="autowonder-artifacts-daily-tmp",
        ),
    )
    with pytest.raises(RuntimeError) as artifact:
        build_object_storage()
    assert "autowonder-artifacts-daily-tmp" in str(artifact.value)


def test_s3_presign_uses_public_endpoint_and_reports_missing_objects() -> None:
    """签名打在公网端点上。404 读作不存在，其他错误保留错误码。"""
    storage = S3ObjectStorage(
        "http://minio-internal.example.com:9000",
        "http://minio.example.com:9000",
        "us-east-1",
        "test-access-key-id",
        "test-access-key-secret",
        True,
    )
    download = urlsplit(storage.presign_get("bucket/object", 600))
    assert download.hostname == "minio.example.com"
    assert download.port == 9000
    assert download.path == "/bucket/object"
    assert download.query is not None
    assert "X-Amz-Signature" in download.query
    upload = urlsplit(storage.presign_put("bucket", "debug/200/DevAgent-run-1.log.gz", 1200))
    assert upload.hostname == "minio.example.com"
    assert upload.path == "/bucket/debug/200/DevAgent-run-1.log.gz"
    assert upload.query is not None
    assert "X-Amz-Expires=1200" in upload.query
    service = _S3Service()
    signed = S3ObjectStorage.from_clients(service, _Signer())
    assert signed.get("bucket/key") == b"\x01\x02\x03"
    service.error = _client_error("NoSuchKey", 404)
    assert signed.get("bucket/key") is None
    service.error = _client_error("InternalError", 500)
    with pytest.raises(ObjectStorageError) as failed:
        signed.get("bucket/key")
    assert failed.value.error_code == "InternalError"
    service.error = None
    assert signed.exists("bucket/key") is True
    service.error = _client_error("404", 404)
    assert signed.exists("bucket/key") is False
    service.error = _client_error("InternalError", 500)
    with pytest.raises(ObjectStorageError) as head:
        signed.exists("bucket/key")
    assert head.value.error_code == "InternalError"
    service.error = RuntimeError("network down")
    with pytest.raises(RuntimeError):
        signed.exists("bucket/key")
    signed.delete("bucket/key")
    service.error = RuntimeError("connection reset")
    with pytest.raises(ObjectStorageError) as wrapped:
        signed.get("bucket/key")
    assert wrapped.value.error_code is None


def test_oss_presign_upgrades_http_and_reads_missing_keys() -> None:
    """下载地址改成 HTTPS。NoSuchKey 返回空，其他 OSS 错误保留错误码。"""
    live = AliyunOssObjectStorage(
        "https://oss-cn-shanghai-internal.aliyuncs.com",
        "https://oss-cn-shanghai.aliyuncs.com",
        "test-access-key-id",
        "test-access-key-secret",
    )
    host = urlsplit(live.presign_get("bucket/object", 600)).hostname
    assert host == "bucket.oss-cn-shanghai.aliyuncs.com"
    service = _OssBucket()
    public = _OssBucket()
    public.signed = "http://bucket.oss-cn-beijing.aliyuncs.com/object?Expires=1&Signature=test"
    storage = AliyunOssObjectStorage(
        "https://internal.example",
        "https://public.example",
        "id",
        "secret",
    )
    storage.bind_clients(service, public)
    assert storage.get("bucket/key") == b"\x0a\x14\x1e"
    assert service.puts == 0
    url = storage.presign_get("bucket/object", 600)
    assert url.startswith("https://bucket.oss-cn-beijing.aliyuncs.com/object?")
    assert "Signature=test" in url
    import oss2.exceptions as oss_errors  # type: ignore[import-untyped]

    service.error = oss_errors.NoSuchKey(404, {}, "", {"Code": "NoSuchKey", "Message": "missing"})
    assert storage.get("bucket/key") is None
    service.error = oss_errors.OssError(
        500,
        {},
        "",
        {"Code": "InternalError", "Message": "later"},
    )
    with pytest.raises(ObjectStorageError) as failed:
        storage.get("bucket/key")
    assert failed.value.error_code == "InternalError"
    service.error = RuntimeError("connection reset")
    with pytest.raises(ObjectStorageError) as wrapped:
        storage.get("bucket/key")
    assert wrapped.value.error_code is None
    service.error = oss_errors.OssError(403, {}, "", {"Code": "AccessDenied", "Message": "no"})
    with pytest.raises(oss_errors.OssError):
        storage.exists("bucket/key")
    service.error = RuntimeError("network down")
    storage.delete("bucket/key")
