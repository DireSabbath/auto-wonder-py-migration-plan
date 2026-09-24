"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class DebugLog(Base):
    """小队 debug 日志登记"""

    __tablename__ = "debug_log"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="WORKITEM / SCHEDULED_TASK_RUN"
    )
    source_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="workitemId 或 scheduledTaskRunId"
    )
    dispatch_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_version_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    run_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="该 source 下该 agent 的第 n 轮"
    )
    dispatch_status: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="SUCCEEDED / FAILED / TIMEOUT / CANCELED"
    )
    object_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="debug/{workitemId}/{roleCode}-run-{n}.log.gz 或 debug/scheduled-{scheduledTaskId}-run-{runId}/{roleCode}-run-{n}.log.gz",
    )
    size_bytes: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="gzip 后实际大小"
    )
    sha256: Mapped[str | None] = mapped_column(
        String(80), nullable=True, comment="裸 64 位 hex；可能吸收 sha256: 前缀形态"
    )
    truncated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    upload_channel: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="DIRECT / RELAY"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="PENDING / UPLOADED / FAILED"
    )
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


register_tenant_model(DebugLog)
