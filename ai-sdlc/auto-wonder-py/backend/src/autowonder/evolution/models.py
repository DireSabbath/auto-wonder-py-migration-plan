"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Double, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base
from autowonder.db.tenant import register_tenant_model


class EvolutionProposal(Base):
    """自进化候选控制点（lean v1）"""

    __tablename__ = "evolution_proposal"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    asset_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="MEMORY/REPO_RELATION/SKILL"
    )
    asset_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="修订已有资产时的资产 ID"
    )
    trigger_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="USER_CORRECTION/SOURCE_INVALIDATED/MOTIF_FAILURE/MANUAL",
    )
    root_evidence_json: Mapped[object] = mapped_column(
        JSON, nullable=False, comment="可追溯 evidence refs，不允许为空"
    )
    policy_json: Mapped[object | None] = mapped_column(
        JSON, nullable=True, comment="Bayesian policy action 与 campaign 上下文"
    )
    candidate_patch_json: Mapped[object] = mapped_column(
        JSON, nullable=False, comment="资产专属候选 patch，不是 active 状态"
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PROPOSED",
        server_default=text("'PROPOSED'"),
        comment="PROPOSED/TRIAL/VALIDATED/REPLAY_PASSED/REPLAY_FAIL/REPLAY_INCONCLUSIVE/APPROVED/REJECTED/RELEASED/ROLLED_BACK",
    )
    lifecycle_json: Mapped[object | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Trial/validation/replay/gates/release/rollback lifecycle payloads",
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


class EvolutionEvidence(Base):
    """自进化证据与 Bayesian Lite 后验快照"""

    __tablename__ = "evolution_evidence"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    asset_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="MEMORY/REPO_RELATION/SKILL"
    )
    asset_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    posterior_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="TRUTH/UTILITY/APPLICABILITY/UPLIFT"
    )
    context_key: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="稀疏 V1 context bucket"
    )
    source_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="HUMAN_REVIEW/DETERMINISTIC_TEST/REPLAY_RESULT/..."
    )
    source_ref: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="可追溯 source ref，如 comment:77 或 artifact:test-log",
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, comment="POSITIVE/NEGATIVE")
    weight: Mapped[float] = mapped_column(Double, nullable=False)
    evidence_json: Mapped[object | None] = mapped_column(JSON, nullable=True)
    dependency_group: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="同源证据分组，用于去重/折扣"
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="Ledger 幂等键"
    )
    alpha: Mapped[float] = mapped_column(Double, nullable=False)
    beta: Mapped[float] = mapped_column(Double, nullable=False)
    posterior_mean: Mapped[float] = mapped_column(Double, nullable=False)
    effective_sample_size: Mapped[float] = mapped_column(Double, nullable=False)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP(3)")
    )
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


register_tenant_model(EvolutionProposal)
register_tenant_model(EvolutionEvidence)
