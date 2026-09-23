"""洞察接口的响应。字段名与 Java VO 一致。"""

from autowonder.core.schema import ApiModel


class CostMetrics(ApiModel):
    """Token 和积分。趋势列表新的日期在前。"""

    total_tokens: int
    avg_tokens_per_task: int
    daily_avg: int
    trend: list[int]
    total_credits: float
    avg_credits_per_task: float
    daily_avg_credits: float
    credits_trend: list[float]


class EfficiencyMetrics(ApiModel):
    """完成率按一位小数，趋势是四舍五入后的整数。"""

    completion_rate: float
    total_tasks: int
    completed_tasks: int
    avg_duration_minutes: int
    trend: list[int]


class StabilityMetrics(ApiModel):
    """没有调度时成功率按 100。"""

    success_rate: float
    retry_count: int
    blocked_count: int
    trend: list[int]


class SecurityMetrics(ApiModel):
    """高风险动作和拦截次数。没有审计时合规率按 100。"""

    high_risk_ops: int
    compliance_rate: float
    audit_blocks: int
    trend: list[int]


class InsightMetricsView(ApiModel):
    """成本、效率、稳定性和安全四块指标。"""

    cost: CostMetrics
    efficiency: EfficiencyMetrics
    stability: StabilityMetrics
    security: SecurityMetrics


class InsightAuditItem(ApiModel):
    """一条审计明细。时间是数据库格式化后的文本。"""

    timestamp: str | None = None
    worker: str | None = None
    event_type: str | None = None
    detail: str | None = None
    risk_level: str | None = None


class InsightAuditPage(ApiModel):
    """审计分页。只有 items 和 total。"""

    items: list[InsightAuditItem]
    total: int


class InsightWorker(ApiModel):
    """出现过调度的数字员工。id 按 Java 写成字符串。"""

    id: str
    name: str | None = None


class DurationSummary(ApiModel):
    """平均时长，单位秒。"""

    total_duration_seconds: int
    human_duration_seconds: int
    agent_duration_seconds: int


class P90Workitem(ApiModel):
    """P90 或最慢尾部中的一张工单。"""

    workitem_id: int
    title: str | None = None
    completed_at: str
    total_duration_seconds: int
    human_duration_seconds: int
    agent_duration_seconds: int


class TrendEntry(ApiModel):
    """一个日期桶的平均时长。"""

    label: str
    average_total_seconds: int
    average_human_seconds: int
    average_agent_seconds: int


class ParticipationView(ApiModel):
    """人机协作汇总。快照还没生成时 available 为 false。"""

    available: bool = False
    generated_at: str | None = None
    data_through: str | None = None
    refresh_triggered: bool = False
    sample_size: int = 0
    average: DurationSummary | None = None
    p90: P90Workitem | None = None
    trend: list[TrendEntry] | None = None


class SlowTailPage(ApiModel):
    """最慢尾部分页。"""

    tail_size: int
    total: int
    page: int
    page_size: int
    items: list[P90Workitem]


class DeliveryCounts(ApiModel):
    """成员交付计数。"""

    member_id: int | None = None
    total: int = 0
    completed: int = 0
    in_progress: int = 0
    requirements: int = 0


class DeliveryMember(DeliveryCounts):
    """带显示名的成员计数。"""

    member_name: str | None = None


class DeliveryReport(ApiModel):
    """成员交付报表。日期按上海日历。"""

    start_date: str
    end_date: str
    timezone: str
    summary: DeliveryCounts
    week_requirements: int
    members: list[DeliveryMember]
