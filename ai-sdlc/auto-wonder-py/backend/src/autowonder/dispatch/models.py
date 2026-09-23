"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Dispatch(Base):
    """调度派发"""

    __tablename__ = "dispatch"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="WORKITEM", server_default=text("'WORKITEM'")
    )
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sdlc_step_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前 SDLC 步骤"
    )
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="目标数字员工")
    agent_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="本次派发使用的在线版本（冻结装配依据）"
    )
    executor_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="选中执行器（派发后填）"
    )
    package_oss_ref: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="任务包 zip 的 OSS 引用"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
        comment="PENDING/PACKAGING/DISPATCHED/ACKED/RUNNING/PAUSING/PAUSED/PAUSE_FAILED/WAITING_FOR_PAUSE/SUCCEEDED/FAILED/TIMEOUT/CANCELED",
    )
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="重试次数"
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="幂等键 = workitemId+stepId+attempt"
    )
    normalized_idempotency_key: Mapped[str | None] = mapped_column(String(137), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="执行结论/总结（无 CONCLUSION 产物时作队友结论）"
    )
    error: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="失败原因")
    resume_from_dispatch_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="恢复或返工复用的来源派发"
    )
    delivery_source_dispatch_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="前序权威交付派发，仅用于继承结论、产物与源码版本"
    )
    resume_mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="RECOVERY/RETURNING_WORKER"
    )
    debug_log_enabled: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="打包时冻结：本轮是否收集全量 debug 日志",
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class DispatchRecoveryCheckpoint(Base):
    """Runtime 可恢复检查点"""

    __tablename__ = "dispatch_recovery_checkpoint"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dispatch_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checkpoint_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_session_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    runtime_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    executor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active_step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oss_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class DispatchRuntimeEvent(Base):
    """派发运行时事件"""

    __tablename__ = "dispatch_runtime_event"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dispatch_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="来源派发")
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="来源数字员工")
    event_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Runtime 幂等事件 ID"
    )
    seq: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="Runtime dispatch 内单调序号"
    )
    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="step.started/step.completed/agent.progress/dispatch.* 等",
    )
    step_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="客户端上报的步骤ID（可空）"
    )
    step_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="客户端上报的步骤编码/键（可空）"
    )
    step_order: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="客户端上报的步骤序号（可空）"
    )
    step_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="客户端上报的步骤名称（可空）"
    )
    message: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="进度摘要/明细"
    )
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="错误摘要")
    detail_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="完整 runtime event 原始 JSON"
    )
    event_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="客户端事件时间（可空）"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class DispatchRecovery(Base):
    """dispatch_recovery"""

    __tablename__ = "dispatch_recovery"

    tenant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    dispatch_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    cancel_requested: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    stop_pending: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    forced: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    modifier_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


register_tenant_model(Dispatch)
