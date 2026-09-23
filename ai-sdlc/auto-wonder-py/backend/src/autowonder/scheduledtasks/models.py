"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class ScheduledTask(Base):
    """7x24 scheduled task definition"""

    __tablename__ = "scheduled_task"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    instruction_md: Mapped[str] = mapped_column(Text, nullable=False)
    squad_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    initial_agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="ONCE/CRON")
    run_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="ONCE UTC instant"
    )
    cron_expression: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Canonical six-field Cron"
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, comment="IANA timezone")
    session_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="ISOLATED",
        server_default=text("'ISOLATED'"),
        comment="ISOLATED/CONTINUOUS",
    )
    overlap_policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="SKIP",
        server_default=text("'SKIP'"),
        comment="SKIP/QUEUE/ALLOW",
    )
    misfire_policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="FIRE_LATEST",
        server_default=text("'FIRE_LATEST'"),
        comment="FIRE_LATEST/FIRE_ALL/SKIP_ALL",
    )
    start_deadline_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=21600, server_default=text("21600")
    )
    affinity_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1800, server_default=text("1800")
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="ACTIVE",
        server_default=text("'ACTIVE'"),
        comment="ACTIVE/PAUSED/EXHAUSTED/ARCHIVED",
    )
    next_fire_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="UTC scheduling cursor"
    )
    last_fire_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Last claimed scheduled instant in UTC"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Task owner")
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class ScheduledTaskRun(Base):
    """7x24 scheduled task execution occurrence"""

    __tablename__ = "scheduled_task_run"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    workspace_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scheduled_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trigger_key: Mapped[str] = mapped_column(String(256), nullable=False)
    trigger_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="SCHEDULED/MANUAL/MISFIRE"
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="Planned UTC instant"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="QUEUED/STARTING/WAITING_EXECUTOR/RUNNING/WAITING_HUMAN/PAUSED/SUCCEEDED/FAILED/TIMED_OUT/CANCELED/SKIPPED",
    )
    skip_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="OVERLAP/MISFIRE_POLICY/START_DEADLINE"
    )
    squad_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    initial_agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sdlc_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_step_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    session_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="ISOLATED/CONTINUOUS"
    )
    resume_from_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    degraded_resume: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    degraded_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    execution_snapshot_json: Mapped[object] = mapped_column(JSON, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
