"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class AiUsage(Base):
    """AI 用量计量"""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False, comment="计量周期，如 2026-07")
    scene: Mapped[str] = mapped_column(String(20), nullable=False, comment="AI 场景或 ALL 汇总")
    call_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"), comment="调用次数"
    )
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class DispatchAiUsage(Base):
    """派发级 AI Token 用量明细"""

    __tablename__ = "dispatch_ai_usage"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workitem_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dispatch_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    executor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    artifact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    step_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=text("''")
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    credits: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    total_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    raw_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    usage_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


class AiQuota(Base):
    """AI 配额"""

    __tablename__ = "ai_quota"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    period_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="MONTH",
        server_default=text("'MONTH'"),
        comment="计量周期类型",
    )
    max_calls: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="周期最大调用次数（空=系统默认）"
    )
    max_tokens: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="周期最大 token"
    )
    concurrency_limit: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="并发上限（驱动 ai:concur 信号量）"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )


register_tenant_model(AiUsage)
register_tenant_model(AiQuota)
