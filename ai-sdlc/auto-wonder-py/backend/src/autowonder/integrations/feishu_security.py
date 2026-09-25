"""飞书回调验签和解密。失败统一变成 SecurityError。"""

import hashlib
import hmac
import json
from base64 import b64decode

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class SecurityError(Exception):
    """回调验签失败。控制器把它收成 401。"""


class FeishuSecrets:
    """绑定里加密保存的三项凭据。"""

    def __init__(
        self,
        app_secret: str | None,
        verification_token: str | None,
        encrypt_key: str | None,
    ) -> None:
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key

    def to_json(self) -> str:
        """字段名与 Java record 的 Jackson 输出一致。"""
        return json.dumps(
            {
                "appSecret": self.app_secret,
                "verificationToken": self.verification_token,
                "encryptKey": self.encrypt_key,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def from_json(payload: str) -> "FeishuSecrets":
        """从密文解密后的 JSON 还原。"""
        parsed = json.loads(payload)
        return FeishuSecrets(
            parsed.get("appSecret"),
            parsed.get("verificationToken"),
            parsed.get("encryptKey"),
        )


def verify_callback(
    body: str | None,
    secrets: FeishuSecrets,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    now_seconds: int,
) -> dict[str, object]:
    """校验令牌、可选加密和签名，返回事件对象。"""
    try:
        if body is None or len(body) > 1_000_000:
            raise ValueError("body")
        envelope = json.loads(body)
        if not isinstance(envelope, dict):
            raise ValueError("envelope")
        key = secrets.encrypt_key
        encrypted = key is not None and key.strip() != ""
        event: object = envelope
        if encrypted:
            raw = b64decode(str(envelope.get("encrypt") or ""))
            if len(raw) < 32 or len(raw) % 16 != 0:
                raise ValueError("cipher")
            event = json.loads(_decrypt(key or "", raw))
        elif "encrypt" in envelope:
            raise ValueError("unexpected encrypt")
        if not isinstance(event, dict):
            raise ValueError("event")
        challenge = event.get("type") == "url_verification"
        token = _path_text(event, "token") if challenge else _header_token(event)
        if not _equal(secrets.verification_token, token):
            raise ValueError("token")
        if encrypted and not challenge:
            if timestamp is None or nonce is None or nonce.strip() == "":
                raise ValueError("nonce")
            moment = int(timestamp)
            if moment < now_seconds - 300 or moment > now_seconds + 300:
                raise ValueError("time")
            material = timestamp + nonce + (key or "") + body
            expected = hashlib.sha256(material.encode()).hexdigest()
            if not _equal(expected, signature):
                raise ValueError("signature")
        return event
    except Exception as error:
        raise SecurityError("invalid Feishu callback") from error


def _decrypt(key: str, raw: bytes) -> str:
    digest = hashlib.sha256(key.encode()).digest()
    iv = raw[:16]
    decryptor = Cipher(algorithms.AES(digest), modes.CBC(iv)).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode()


def _header_token(event: dict[str, object]) -> str:
    header = event.get("header")
    if not isinstance(header, dict):
        return ""
    token = header.get("token")
    return "" if token is None else str(token)


def _path_text(event: dict[str, object], key: str) -> str:
    value = event.get(key)
    return "" if value is None else str(value)


def _equal(expected: str | None, actual: str | None) -> bool:
    if expected is None or expected.strip() == "" or actual is None:
        return False
    left = expected.encode()
    right = actual.encode()
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)
