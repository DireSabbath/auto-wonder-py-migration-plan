"""S3 兼容对象存储。服务端走内部端点，签名走公网端点。"""

import logging
from typing import Any

from autowonder.storage.objects import ObjectStorageError, StoredObject, md5_hex, split_oss_ref

logger = logging.getLogger(__name__)


class S3ObjectStorage:
    """MinIO 或 AWS S3。校验和只在服务端要求时计算，以兼容旧版 MinIO。"""

    def __init__(
        self,
        endpoint: str,
        public_endpoint: str,
        region: str,
        access_key_id: str,
        access_key_secret: str,
        force_path_style: bool,
    ) -> None:
        self._service = _client(
            endpoint,
            region,
            access_key_id,
            access_key_secret,
            force_path_style,
        )
        self._signer = _client(
            public_endpoint,
            region,
            access_key_id,
            access_key_secret,
            force_path_style,
        )

    @classmethod
    def from_clients(cls, service: Any, signer: Any) -> "S3ObjectStorage":
        """测试注入已经建好的服务客户端和签名客户端。"""
        storage = object.__new__(cls)
        storage._service = service
        storage._signer = signer
        return storage

    def put(self, bucket: str, key: str, data: bytes) -> StoredObject:
        """写入对象。失败时带上 S3 错误码。"""
        try:
            self._service.put_object(Bucket=bucket, Key=key, Body=data)
        except Exception as error:
            code = _error_code(error)
            logger.error(
                "s3 put failed bucket=%s key=%s errorCode=%s",
                bucket,
                key,
                code,
                exc_info=True,
            )
            raise ObjectStorageError("s3 put failed", bucket, key, code) from error
        return StoredObject(bucket + "/" + key, md5_hex(data), len(data))

    def get(self, oss_ref: str) -> bytes | None:
        """读取对象。不存在时返回空。"""
        bucket, key = split_oss_ref(oss_ref)
        try:
            response = self._service.get_object(Bucket=bucket, Key=key)
            body = response["Body"].read()
        except Exception as error:
            if _missing(error):
                logger.info("s3 object not found ref=%s", oss_ref)
                return None
            code = _error_code(error)
            logger.error(
                "s3 get failed ref=%s errorCode=%s",
                oss_ref,
                code,
                exc_info=True,
            )
            raise ObjectStorageError("s3 get failed", bucket, key, code) from error
        if isinstance(body, bytes):
            return body
        return bytes(body)

    def presign_get(self, oss_ref: str, ttl_seconds: int) -> str:
        """用公网端点签发下载地址。"""
        bucket, key = split_oss_ref(oss_ref)
        return str(
            self._signer.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )
        )

    def presign_put(self, bucket: str, key: str, ttl_seconds: int) -> str:
        """用公网端点签发上传地址。"""
        return str(
            self._signer.generate_presigned_url(
                "put_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )
        )

    def exists(self, oss_ref: str) -> bool:
        """HEAD 成功即为存在。404 视为不存在。"""
        bucket, key = split_oss_ref(oss_ref)
        try:
            self._service.head_object(Bucket=bucket, Key=key)
        except Exception as error:
            if _missing(error):
                return False
            if _error_code(error) is None and _status(error) is None:
                raise
            raise ObjectStorageError(
                "s3 head failed",
                bucket,
                key,
                _error_code(error),
            ) from error
        return True

    def delete(self, oss_ref: str) -> None:
        """删除失败只记日志。"""
        bucket, key = split_oss_ref(oss_ref)
        try:
            self._service.delete_object(Bucket=bucket, Key=key)
        except Exception:
            logger.warning("s3 delete failed ref=%s", oss_ref, exc_info=True)


def _client(
    endpoint: str,
    region: str,
    access_key_id: str,
    access_key_secret: str,
    force_path_style: bool,
) -> Any:
    import boto3  # type: ignore[import-untyped]
    from botocore.client import Config  # type: ignore[import-untyped]

    style = "virtual"
    if force_path_style:
        style = "path"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=access_key_secret,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": style},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _response(error: BaseException) -> dict[str, Any] | None:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return response
    return None


def _error_code(error: BaseException) -> str | None:
    response = _response(error)
    if response is None:
        return None
    info = response.get("Error")
    if not isinstance(info, dict):
        return None
    code = info.get("Code")
    if isinstance(code, str):
        return code
    return None


def _status(error: BaseException) -> int | None:
    response = _response(error)
    if response is None:
        return None
    meta = response.get("ResponseMetadata")
    if not isinstance(meta, dict):
        return None
    status = meta.get("HTTPStatusCode")
    if isinstance(status, int):
        return status
    return None


def _missing(error: BaseException) -> bool:
    if _status(error) == 404:
        return True
    code = _error_code(error)
    return code == "NoSuchKey" or code == "404"
