"""BCrypt ``$2a$``，与 Spring ``BCryptPasswordEncoder`` 互通。"""

import bcrypt


def encode(raw: str) -> str:
    """生成强度 10 的 ``$2a$`` 哈希。"""
    hashed = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=10, prefix=b"2a"))
    return hashed.decode()


def matches(raw: str, hashed: str) -> bool:
    """校验明文与已有哈希。非 BCrypt 文本（含注销哨兵）视为不匹配。"""
    try:
        return bcrypt.checkpw(raw.encode(), hashed.encode())
    except ValueError:
        return False
