"""Aone 开关、签名和表单编码。默认关闭时请求仍先进入这里再拒绝。"""

import base64
import os
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from autowonder.config import get_settings


class AoneDisabledError(RuntimeError):
    """对齐 ``AoneDisabledException``。社区版默认关闭时远程调用抛出它。"""

    def __init__(self, message: str = "Aone integration is disabled") -> None:
        super().__init__(message)


class AoneOpenApiError(RuntimeError):
    """Aone 返回失败或非 JSON。``terminal`` 表示重试也不会成功。"""

    def __init__(self, message: str, terminal: bool = False) -> None:
        self.terminal = terminal
        super().__init__(message)


def aone_enabled() -> bool:
    """读取 ``AUTOWONDER_AONE_ENABLED``。缺省与 Java ``enabled: false`` 一致。"""
    return get_settings().aone_enabled


def aone_web_base_url() -> str | None:
    """深链根地址。未配置时不把环境主机写进产物。"""
    value = os.environ.get("AUTOWONDER_AONE_WEB_BASE_URL", "")
    if value.strip() == "":
        return None
    return value.strip().rstrip("/")


def require_enabled() -> None:
    """远程调用前的开关。关闭时抛出，不改走成功返回。"""
    if not aone_enabled():
        raise AoneDisabledError()


def sign_aone(app_name: str, app_secret: str, timestamp: int) -> str:
    """AES/ECB/PKCS5，输出去掉填充的 URL-safe Base64。"""
    content = f"appName={app_name};timestamp={timestamp}"
    key = base64.b64decode(app_secret)
    pad = 16 - (len(content.encode()) % 16)
    padded = content.encode() + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    token = base64.b64encode(encrypted).decode()
    return token.replace("+", "-").replace("/", "_").rstrip("=")


def to_url_encoded_query(params: dict[str, object]) -> str:
    """按 Java ``URLEncoder`` 拼表单。空集合和 null 不出现。"""
    parts: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        serialized = serialize_query_value(value)
        if serialized == "" or serialized == "[]":
            continue
        parts.append(java_url_encode(str(key)) + "=" + java_url_encode(serialized))
    return "&".join(parts)


def serialize_query_value(value: object) -> str:
    """集合写成 JSON 数组文本，标量用 ``String.valueOf``。"""
    if isinstance(value, list | tuple | set):
        items: list[str] = []
        for item in value:
            if isinstance(item, bool) or isinstance(item, int | float):
                items.append(str(item).lower() if isinstance(item, bool) else str(item))
            else:
                items.append('"' + str(item).replace("\\", "\\\\").replace('"', '\\"') + '"')
        return "[" + ",".join(items) + "]"
    return str(value)


def java_url_encode(value: str) -> str:
    """保留 ``.-*_``，空格写成 ``+``。"""
    return quote(value, safe=".-*_").replace("%20", "+")
