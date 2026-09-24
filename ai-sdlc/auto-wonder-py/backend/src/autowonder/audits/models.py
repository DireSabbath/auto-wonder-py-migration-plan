"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class AuditLog(Base):
    """审计/操作日志"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="操作人 user_id"
    )
    module: Mapped[str] = mapped_column(String(64), nullable=False, comment="模块")
    action: Mapped[str] = mapped_column(String(64), nullable=False, comment="动作")
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    detail_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="细节（密钥/凭据已脱敏）"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
