"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class Sdlc(Base):
    """SDLC 流程定义"""

    __tablename__ = "sdlc"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    work_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="REQ/TASK/BUG（可空=通用）"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="DRAFT",
        server_default=text("'DRAFT'"),
        comment="DRAFT/ENABLED/DISABLED",
    )
    is_default: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    entry_step_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="入口步骤（= 最小 order 步）"
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


class SdlcStep(Base):
    """SDLC 步骤"""

    __tablename__ = "sdlc_step"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sdlc_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    step_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="步序（同 sdlc 内唯一、单调；软删除行置为 -id 释放正数占位）",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="内部步骤类型：analysis/implementation/test/handoff 等"
    )
    instruction_md: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="给数字员工执行本步骤的详细说明"
    )
    checklist_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="执行检查项数组"
    )
    gate_policy_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="步骤准入/准出策略"
    )
    required: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"), comment="是否必需步骤"
    )
    timeout_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="步骤建议超时时间"
    )
    retry_budget: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="步骤建议重试预算"
    )
    code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="废弃：旧步骤编码")
    handler_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="废弃：旧 AGENT/HUMAN 路由字段"
    )
    handler_role_ref: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="废弃：旧目标角色码"
    )
    status_on_enter_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="废弃：旧进入状态"
    )
    on_success: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="废弃：旧成功流转"
    )
    on_fail: Mapped[object | None] = mapped_column(JSON, nullable=True, comment="废弃：旧失败流转")
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


register_tenant_model(Sdlc)
register_tenant_model(SdlcStep)
