"""AI 用量和配额的请求与响应。"""

from autowonder.core.schema import ApiModel


class AiUsageView(ApiModel):
    """一个周期、一个场景的用量。计数字段总是写出。"""

    period: str
    scene: str
    call_count: int
    input_tokens: int
    output_tokens: int


class AiQuotaView(ApiModel):
    """工作空间配额。没有记录时周期类型为 MONTH，上限为空。"""

    period_type: str | None = None
    max_calls: int | None = None
    max_tokens: int | None = None
    concurrency_limit: int | None = None


class UpdateQuotaRequest(ApiModel):
    """更新配额。null 会清空对应上限。"""

    max_calls: int | None = None
    max_tokens: int | None = None
    concurrency_limit: int | None = None
