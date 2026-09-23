"""执行器令牌。``b64:`` 保存原文，其余按盐值 SHA-256 比对。"""

import base64
import hashlib
import hmac

_SALT = "autowonder-executor"
_PREFIX_B64 = "b64:"
_PREFIX_SHA = "sha256:"


def validate(token_ref: str | None, plaintext: str | None) -> bool:
    """比对执行器持有的明文和库里的引用。任一为空则不通过。"""
    if token_ref is None or plaintext is None:
        return False
    if token_ref.startswith(_PREFIX_B64):
        stored = resolve(token_ref)
        if stored is None:
            return False
        return hmac.compare_digest(stored.encode("utf-8"), plaintext.encode("utf-8"))
    expected = _PREFIX_SHA + _sha256_hex(_SALT + plaintext)
    return hmac.compare_digest(expected.encode("utf-8"), token_ref.encode("utf-8"))


def resolve(token_ref: str | None) -> str | None:
    """解开 ``b64:`` 引用。其他前缀没有可还原的明文。"""
    if token_ref is None or not token_ref.startswith(_PREFIX_B64):
        return None
    raw = base64.b64decode(token_ref[len(_PREFIX_B64) :], validate=True)
    return raw.decode("utf-8")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
