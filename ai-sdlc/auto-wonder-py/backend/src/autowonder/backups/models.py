"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class ProjectBackup(Base):
    """项目配置备份历史"""

    __tablename__ = "project_backup"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="RUNNING / SUCCEEDED / FAILED"
    )
    oss_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_finished: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
