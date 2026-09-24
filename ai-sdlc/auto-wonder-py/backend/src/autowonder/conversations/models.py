"""由 scripts/generate_models.py 按 schema 契约生成。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from autowonder.core.clock import now_local
from autowonder.db.base import Base


class AgentConversation(Base):
    """数字人会话线程"""

    __tablename__ = "agent_conversation"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="PLATFORM_ASSISTANT immutable owner"
    )
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="最近一轮使用的在线 AgentVersion"
    )
    channel: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="DINGTALK / WORKITEM_CLARIFICATION / PLATFORM_ASSISTANT etc.",
    )
    biz_ref_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="WORKITEM etc."
    )
    biz_ref_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="workitem id etc."
    )
    channel_conversation_id: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="钉钉 openConversationId or opaque UUID"
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title_source: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="AUTO/USER"
    )
    cli_session_ref: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="CLI 会话 id,用于 --resume"
    )
    executor_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="粘性 executor"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    last_turn_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class AgentConversationTurn(Base):
    """会话 turn(入站/出站)"""

    __tablename__ = "agent_conversation_turn"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, comment="IN / OUT")
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_msg_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="入站幂等唯一键"
    )
    request_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="入站 HTTP requestId,用于异步回包日志串联"
    )
    source_context: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON source context for inbound channel reply delivery"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    last_dispatch_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最近一次向 runtime 投递该 turn 的时间"
    )
    dispatch_attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="向 runtime 投递该 turn 的次数",
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )


class AgentConversationTurnEvent(Base):
    """Provider event chunks for conversation turns"""

    __tablename__ = "agent_conversation_turn_event"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dispatch_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_fragment: Mapped[str] = mapped_column(Text, nullable=False)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )


class AgentConversationElicitation(Base):
    """ACP 问答卡片挂起请求"""

    __tablename__ = "agent_conversation_elicitation"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="执行器生成的挂起请求标识"
    )
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="form", server_default=text("'form'")
    )
    message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    schema_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="ACP requestedSchema 原样存储"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
        comment="PENDING/ANSWERED/DECLINED/EXPIRED/CANCELED",
    )
    answer_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )


class ConversationShare(Base):
    """平台会话只读分享"""

    __tablename__ = "conversation_share"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    grantee_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    permission: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="READ",
        server_default=text("'READ'"),
        comment="本期只有 READ，分享用户不能续聊",
    )
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="只能是会话 Owner")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )


class ConversationTurnArtifact(Base):
    """Turn 与文件的不可变引用"""

    __tablename__ = "conversation_turn_artifact"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, comment="INPUT / OUTPUT")
    reference_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="UPLOAD / SELECTED / MENTION / GENERATED"
    )
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    manifest_json: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Turn 创建时固化的附件清单，后续同名文件更新不得回溯改写"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )


class ConversationActionPlan(Base):
    """参数冻结的一次性平台动作计划"""

    __tablename__ = "conversation_action_plan"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workspace_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="执行时复核权限用的工作空间"
    )
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="只有此人能确认，跨 Owner 确认必须失败"
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="DRAFT",
        server_default=text("'DRAFT'"),
        comment="DRAFT/PENDING_CONFIRMATION/APPROVED/EXECUTING/SUCCEEDED/PARTIAL_FAILED/REJECTED/EXPIRED/CANCELED",
    )
    canonical_payload_json: Mapped[str] = mapped_column(
        Text, nullable=False, comment="确定性 JSON Canonicalization 后的冻结参数"
    )
    payload_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="确认时必须回传同一哈希，防篡改"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="TTL 到期即 EXPIRED，不可确认"
    )
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="原子消费标记，非空即不可再次执行"
    )
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class ConversationActionStep(Base):
    """动作计划的冻结步骤与真实执行结果"""

    __tablename__ = "conversation_action_step"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="冻结的工具名，执行时不接受替换"
    )
    arguments_json: Mapped[str] = mapped_column(
        Text, nullable=False, comment="冻结的参数，来自计划而非 Runtime 回包"
    )
    arguments_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="确定性幂等键，仅标记幂等的失败步骤可重试"
    )
    idempotent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="1 表示失败后可安全重试",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
        comment="PENDING/RUNNING/SUCCEEDED/FAILED/SKIPPED",
    )
    result_summary_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="脱敏结果摘要，不含正文与 Secret"
    )
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gmt_create: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
    gmt_modified: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local, server_default=text("CURRENT_TIMESTAMP")
    )
