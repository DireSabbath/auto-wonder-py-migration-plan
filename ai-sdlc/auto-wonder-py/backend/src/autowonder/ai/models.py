"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class AiSession(Base):
    """AI 会话"""

    __tablename__ = "ai_session"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scene: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="REPO_SCAN/MEMORY_IMPORT/SDLC_GEN/CLARIFICATION"
    )
    biz_ref_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="REPO/MEMORY/SDLC/WORKITEM/NONE"
    )
    biz_ref_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="QUEUED",
        server_default=text("'QUEUED'"),
        comment="QUEUED/RUNNING/WAIT_USER/COMPLETED/FAILED/CANCELED",
    )
    cli_session_ref: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="CLI 侧会话标识（--resume）"
    )
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="执行节点")
    result_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="最新结构化结果（待确认）"
    )
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
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


class AiMessage(Base):
    """AI 消息"""

    __tablename__ = "ai_message"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="会话内序号")
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="USER/AI/SYSTEM")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="引用/附件/结构片段"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


register_tenant_model(AiSession)
register_tenant_model(AiMessage)
