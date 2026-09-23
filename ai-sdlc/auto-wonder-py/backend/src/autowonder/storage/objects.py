"""对象存储引用。``oss_ref`` 形如 ``{bucket}/{key}``。"""

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredObject:
    """一次写入的引用、MD5 和字节数。"""

    oss_ref: str
    md5: str
    size: int


class ObjectStorage(Protocol):
    """与 Java ``ObjectStorage`` 对齐的存储操作。"""

    def put(self, bucket: str, key: str, data: bytes) -> StoredObject:
        """写入对象并返回引用。"""

    def get(self, oss_ref: str) -> bytes | None:
        """按引用读取；不存在时返回空。"""

    def presign_get(self, oss_ref: str, ttl_seconds: int) -> str:
        """生成限时下载地址。"""

    def exists(self, oss_ref: str) -> bool:
        """引用当前是否存在。"""

    def delete(self, oss_ref: str) -> None:
        """按引用删除；不存在时不做任何事。"""


class InMemoryObjectStorage:
    """进程内存储。备份创建会拒绝它，因为归档需要可持久化的桶。"""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, bucket: str, key: str, data: bytes) -> StoredObject:
        oss_ref = bucket + "/" + key
        self._store[oss_ref] = bytes(data)
        return StoredObject(oss_ref, md5_hex(data), len(data))

    def get(self, oss_ref: str) -> bytes | None:
        data = self._store.get(oss_ref)
        if data is None:
            return None
        return bytes(data)

    def presign_get(self, oss_ref: str, ttl_seconds: int) -> str:
        return "mem://" + oss_ref + "?ttl=" + str(ttl_seconds)

    def exists(self, oss_ref: str) -> bool:
        return oss_ref in self._store

    def delete(self, oss_ref: str) -> None:
        self._store.pop(oss_ref, None)


class MapObjectStorage:
    """测试和显式注入用的内存映射。它不是 ``InMemoryObjectStorage``。"""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self.gets: list[str] = []
        self.puts: list[tuple[str, str]] = []

    def put(self, bucket: str, key: str, data: bytes) -> StoredObject:
        oss_ref = bucket + "/" + key
        self._store[oss_ref] = bytes(data)
        self.puts.append((bucket, key))
        return StoredObject(oss_ref, md5_hex(data), len(data))

    def get(self, oss_ref: str) -> bytes | None:
        self.gets.append(oss_ref)
        data = self._store.get(oss_ref)
        if data is None:
            return None
        return bytes(data)

    def presign_get(self, oss_ref: str, ttl_seconds: int) -> str:
        return "mem://" + oss_ref + "?ttl=" + str(ttl_seconds)

    def exists(self, oss_ref: str) -> bool:
        return oss_ref in self._store

    def delete(self, oss_ref: str) -> None:
        self._store.pop(oss_ref, None)


_MEMORY = InMemoryObjectStorage()


def get_object_storage() -> ObjectStorage:
    """尚未接通 OSS/S3 客户端时返回进程内存储。"""
    return _MEMORY


def md5_hex(data: bytes) -> str:
    """小写十六进制 MD5。"""
    return hashlib.md5(data).hexdigest()


def sha256_hex(data: bytes) -> str:
    """小写十六进制 SHA-256。"""
    return hashlib.sha256(data).hexdigest()


def has_text(value: str | None) -> bool:
    """非空且含非空白字符。"""
    if value is None:
        return False
    return value.strip() != ""
