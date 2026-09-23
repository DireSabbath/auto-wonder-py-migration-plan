"""字节级移植 ``AesGcmSecretCrypto``：``enc:v1:`` + url-safe Base64(nonce ‖ 密文 ‖ tag)。"""

import base64
import os
from collections.abc import Callable

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16


class AesGcmSecretCrypto:
    """用配置中的 Base64 主密钥做 AES-GCM。"""

    def __init__(
        self,
        master_key_b64: str,
        nonce_source: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._key = _decode_key(master_key_b64)
        self._nonce_source = nonce_source

    def encrypt(self, plaintext: str) -> str:
        """加密明文，输出 ``enc:v1:`` 信封。"""
        nonce = self._nonce_source(NONCE_BYTES)
        encrypted = AESGCM(self._key).encrypt(nonce, plaintext.encode(), None)
        envelope = nonce + encrypted
        token = base64.urlsafe_b64encode(envelope).decode().rstrip("=")
        return PREFIX + token

    def decrypt(self, ciphertext: str) -> str:
        """解密 ``enc:v1:`` 信封。认证失败时抛出 ``ValueError``。"""
        envelope = _decode_envelope(ciphertext)
        nonce = envelope[:NONCE_BYTES]
        encrypted = envelope[NONCE_BYTES:]
        try:
            plain = AESGCM(self._key).decrypt(nonce, encrypted, None)
        except Exception as error:
            raise ValueError("encrypted secret authentication failed") from error
        return plain.decode()

    def mask(self, value: str | None) -> str:
        """展示用掩码：保留首尾各 2 个字符，短值一律 ``****``。"""
        if value is None or len(value) <= 4:
            return "****"
        return value[:2] + "****" + value[-2:]


def _decode_key(master_key_b64: str) -> bytes:
    if master_key_b64 is None or master_key_b64.strip() == "":
        raise ValueError("secret crypto master key is required")
    try:
        decoded = base64.b64decode(master_key_b64)
    except Exception as error:
        raise ValueError("secret crypto master key must be valid Base64") from error
    if len(decoded) != KEY_BYTES:
        raise ValueError("secret crypto master key must decode to 32 bytes")
    return decoded


def _decode_envelope(ciphertext: str) -> bytes:
    if ciphertext is None or not ciphertext.startswith(PREFIX):
        raise ValueError("invalid encrypted secret envelope")
    encoded = ciphertext[len(PREFIX) :]
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded + padding)
    except Exception as error:
        raise ValueError("invalid encrypted secret envelope") from error
    if len(decoded) < NONCE_BYTES + TAG_BYTES:
        raise ValueError("invalid encrypted secret envelope")
    return decoded
