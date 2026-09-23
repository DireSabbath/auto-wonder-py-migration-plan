"""阿里云 OSS。读写走服务端点，预签名走公网端点，下载地址改成 HTTPS。"""

import logging
from typing import Any

from autowonder.storage.objects import ObjectStorageError, StoredObject, md5_hex, split_oss_ref

logger = logging.getLogger(__name__)


class AliyunOssObjectStorage:
    """一对 OSS 客户端：内部端点负责数据，公网端点只负责签名。"""

    def __init__(
        self,
        endpoint: str,
        public_endpoint: str,
        access_key_id: str,
        access_key_secret: str,
    ) -> None:
        import oss2  # type: ignore[import-untyped]

        self._auth = oss2.Auth(access_key_id, access_key_secret)
        self._service_endpoint = endpoint
        self._public_endpoint = public_endpoint
        self._bound_service: Any = None
        self._bound_public: Any = None

    def bind_clients(self, service_bucket: Any, public_bucket: Any) -> None:
        """测试注入已经建好的桶客户端，不再访问真实端点。"""
        self._bound_service = service_bucket
        self._bound_public = public_bucket

    def put(self, bucket: str, key: str, data: bytes) -> StoredObject:
        """写入对象。失败时带上 OSS 错误码。"""
        try:
            self._service(bucket).put_object(key, data)
        except Exception as error:
            code = _oss_code(error)
            logger.error(
                "oss put failed bucket=%s key=%s errorCode=%s",
                bucket,
                key,
                code,
                exc_info=True,
            )
            raise ObjectStorageError("oss put failed", bucket, key, code) from error
        return StoredObject(bucket + "/" + key, md5_hex(data), len(data))

    def get(self, oss_ref: str) -> bytes | None:
        """读取对象。NoSuchKey 或 404 返回空。"""
        bucket, key = split_oss_ref(oss_ref)
        try:
            result = self._service(bucket).get_object(key)
            payload = result.read()
        except Exception as error:
            code = _oss_code(error)
            status = _oss_status(error)
            if code == "NoSuchKey" or code == "404" or status == 404:
                logger.info("oss object not found ref=%s", oss_ref)
                return None
            if code is None and status is None:
                logger.error(
                    "oss get failed ref=%s errorType=%s",
                    oss_ref,
                    type(error).__name__,
                    exc_info=True,
                )
            else:
                logger.error(
                    "oss get failed ref=%s errorCode=%s",
                    oss_ref,
                    code,
                    exc_info=True,
                )
            raise ObjectStorageError("oss get failed", bucket, key, code) from error
        if isinstance(payload, bytes):
            return payload
        return bytes(payload)

    def presign_get(self, oss_ref: str, ttl_seconds: int) -> str:
        """公网端点签名，并把 http 下载地址改成 https。"""
        bucket, key = split_oss_ref(oss_ref)
        url = self._public(bucket).sign_url("GET", key, ttl_seconds)
        return _force_https(str(url))

    def presign_put(self, bucket: str, key: str, ttl_seconds: int) -> str:
        """公网端点签发 PUT 地址，保留客户端返回的协议。"""
        url = self._public(bucket).sign_url("PUT", key, ttl_seconds)
        return str(url)

    def exists(self, oss_ref: str) -> bool:
        """对象是否存在。存储错误原样抛出。"""
        bucket, key = split_oss_ref(oss_ref)
        return bool(self._service(bucket).object_exists(key))

    def delete(self, oss_ref: str) -> None:
        """删除失败只记日志。"""
        bucket, key = split_oss_ref(oss_ref)
        try:
            self._service(bucket).delete_object(key)
        except Exception:
            logger.warning("oss delete failed ref=%s", oss_ref, exc_info=True)

    def _service(self, bucket: str) -> Any:
        if self._bound_service is not None:
            return self._bound_service
        import oss2

        return oss2.Bucket(self._auth, self._service_endpoint, bucket)

    def _public(self, bucket: str) -> Any:
        if self._bound_public is not None:
            return self._bound_public
        import oss2

        return oss2.Bucket(self._auth, self._public_endpoint, bucket)


def _force_https(url: str) -> str:
    if url[:7].lower() == "http://":
        return "https://" + url[7:]
    return url


def _oss_code(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code != "":
        return code
    return None


def _oss_status(error: BaseException) -> int | None:
    status = getattr(error, "status", None)
    if isinstance(status, int):
        return status
    return None
