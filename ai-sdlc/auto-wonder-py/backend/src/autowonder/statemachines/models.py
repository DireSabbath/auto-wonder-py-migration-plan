"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class StatusTemplate(Base):
    """状态模版"""

    __tablename__ = "status_template"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    work_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="REQ/TASK/BUG")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_default: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="该类型默认模版"
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


class StatusNode(Base):
    """状态节点"""

    __tablename__ = "status_node"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    template_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="如 new/developing/verifying/released"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="INIT/IN_PROGRESS/DONE/CANCELED"
    )
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class StatusTransition(Base):
    """状态迁移边"""

    __tablename__ = "status_transition"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    template_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    from_node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


register_tenant_model(StatusTemplate)
register_tenant_model(StatusNode)
register_tenant_model(StatusTransition)
