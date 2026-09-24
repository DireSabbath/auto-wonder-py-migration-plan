"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class SystemSetting(Base):
    """系统设置"""

    __tablename__ = "system_setting"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    setting_group: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="AI/STORAGE/NOTIFY/DEFAULTS"
    )
    setting_key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="is_secret=1 时不落明文"
    )
    is_secret: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    credential_ref: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="is_secret 时的 SecretCrypto 密文"
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


register_tenant_model(SystemSetting)
