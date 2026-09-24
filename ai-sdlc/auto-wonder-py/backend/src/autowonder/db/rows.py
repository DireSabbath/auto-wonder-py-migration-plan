"""把 SQLAlchemy 执行结果上的影响行数收成 int。"""

from typing import Any, cast

from sqlalchemy.engine import CursorResult


def rowcount(result: object) -> int:
    """MySQL UPDATE/DELETE 返回的变更行数。"""
    return int(cast(CursorResult[Any], result).rowcount)
