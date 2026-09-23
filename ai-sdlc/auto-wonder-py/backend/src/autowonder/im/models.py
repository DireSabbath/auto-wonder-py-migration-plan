"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class PlatformImSelection(Base):
    """Selected platform collaboration notification provider"""

    __tablename__ = "platform_im_selection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)


class PlatformImChannelConfig(Base):
    """平台级 IM 指派通知通道配置"""

    __tablename__ = "platform_im_channel_config"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="IM provider canonical key"
    )
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"), comment="是否启用"
    )
    app_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Provider application key"
    )
    credential_ref: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="SecretCrypto 加密密文"
    )
    robot_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="机器人编码"
    )
    base_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="Provider API base URL"
    )
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class UserImIdentity(Base):
    """用户 IM 身份（全局）"""

    __tablename__ = "user_im_identity"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="全局 user_id")
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="IM provider canonical key"
    )
    external_user_id: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="用户在 IM provider 中的身份"
    )
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    is_deleted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
