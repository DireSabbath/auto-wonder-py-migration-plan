"""分页信封，字段名对齐 Java ``PageResult``。"""

from typing import Any

from pydantic import Field

from autowonder.core.schema import ApiModel


class PageResult(ApiModel):
    """``list`` / ``total`` / ``pageNum`` / ``pageSize``。"""

    list_: list[Any] = Field(alias="list")
    total: int
    page_num: int
    page_size: int
