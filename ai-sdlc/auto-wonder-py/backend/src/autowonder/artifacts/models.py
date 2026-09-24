"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class Artifact(Base):
    """产物"""

    __tablename__ = "artifact"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="WORKITEM", server_default=text("'WORKITEM'")
    )
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dispatch_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="来源派发")
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="FILE/LOG/PATCH/REPORT/CONCLUSION..."
    )
    oss_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    meta_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
