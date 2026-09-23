"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class Executor(Base):
    """执行器接入"""

    __tablename__ = "executor"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="归属数字员工")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_ref: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="可解析的 WS 鉴权 token 引用"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="OFFLINE",
        server_default=text("'OFFLINE'"),
        comment="OFFLINE/ONLINE/BUSY",
    )
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="客户端进程启动时间"
    )
    last_connect_ip: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最近一次成功 WebSocket 接入 IP"
    )
    client_kind: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="客户端形态（claude-cli/qoder-cli）"
    )
    launch_config: Mapped[object | None] = mapped_column(
        JSON,
        nullable=True,
        comment="启动配置 JSON: {model, reasoningEffort, contextWindow, memoryMode}",
    )
    config_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
        comment="启动配置乐观锁版本号",
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


class ExecutorUpdateTask(Base):
    """执行器升级任务"""

    __tablename__ = "executor_update_task"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    executor_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="目标执行器")
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="升级指令关联 ID，客户端幂等键"
    )
    current_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="发起时执行器最近上报版本"
    )
    target_version: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="目标版本（全局推荐版本）"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
        comment="PENDING/DRAINING/UPDATING/SUCCESS/FAILED",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="已执行的升级尝试次数",
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default=text("3")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="本次尝试的截止时间；到期仍未收到客户端上报即视为一次失败"
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近一次下发升级指令的时间；NULL 表示还在等待下发"
    )
    last_error: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="最近一次失败原因"
    )
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="MANUAL",
        server_default=text("'MANUAL'"),
        comment="MANUAL/BATCH/AUTO",
    )
    requested_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
