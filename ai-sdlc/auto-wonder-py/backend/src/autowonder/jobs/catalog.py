"""18 个定时任务的触发契约。

间隔数字与 Java ``@Scheduled(fixedDelay)`` 的默认值一致：上一次结束后再等这段时间。
``initial_delay_seconds`` 对应 ``initialDelay``，缺省表示立即执行第一次。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledJob:
    """一个集群定时任务的名称与触发方式。"""

    name: str
    trigger: str
    every_seconds: float | None = None
    cron: str | None = None
    timezone: str | None = None
    initial_delay_seconds: float | None = None


SCHEDULED_JOBS = (
    ScheduledJob("scheduled_task_scan", "interval", every_seconds=10),
    ScheduledJob("scheduled_task_compensation", "interval", every_seconds=30),
    ScheduledJob("executor_update_scan", "interval", every_seconds=60),
    ScheduledJob("provider_model_catalog_refresh", "interval", every_seconds=86400),
    ScheduledJob("account_deactivation_expiry", "interval", every_seconds=60),
    ScheduledJob("conversation_turn_event_cleanup", "interval", every_seconds=3600),
    ScheduledJob("agent_conversation_recovery", "interval", every_seconds=60),
    ScheduledJob("conversation_elicitation_expiry", "interval", every_seconds=60),
    ScheduledJob("debug_log_reconciliation", "interval", every_seconds=3600),
    ScheduledJob("ai_compensation", "interval", every_seconds=60),
    ScheduledJob("workspace_cleanup", "interval", every_seconds=3600),
    ScheduledJob("dispatch_compensation", "interval", every_seconds=30),
    ScheduledJob("aone_outbox_dispatch", "interval", every_seconds=3),
    ScheduledJob("aone_inbound_poll", "interval", every_seconds=3),
    ScheduledJob(
        "feishu_inbox_drain",
        "interval",
        every_seconds=3,
        initial_delay_seconds=15,
    ),
    ScheduledJob("dingtalk_stream_reconcile", "interval", every_seconds=30),
    ScheduledJob("external_operation_recovery", "interval", every_seconds=5),
    ScheduledJob(
        "human_agent_participation_snapshot",
        "cron",
        cron="0 0 3 * * *",
        timezone="Asia/Shanghai",
    ),
)
