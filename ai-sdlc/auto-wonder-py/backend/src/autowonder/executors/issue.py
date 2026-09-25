"""签发可回显的执行器令牌。引用格式与 ``tokens.resolve`` 的 ``b64:`` 前缀一致。"""

import base64
import secrets


def issue_executor_token(executor_id: int) -> tuple[str, str]:
    """返回明文和 ``b64:`` 引用。明文只在创建当次交给调用方。"""
    raw = secrets.token_bytes(32)
    suffix = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    plaintext = f"exec_{executor_id}_{suffix}"
    token_ref = "b64:" + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")
    return plaintext, token_ref
