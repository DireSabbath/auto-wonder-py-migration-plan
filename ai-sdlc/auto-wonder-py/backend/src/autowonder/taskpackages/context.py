"""任务包装配结果。字段与 Java ``PackageContext`` / ``TaskPackageResult`` 对应。"""

from dataclasses import dataclass, field


@dataclass
class TaskComment:
    """评论快照。正文按 UTF-8 单独落成 ``context/comments/{id}.md``。"""

    id: int
    author_type: str | None
    author_ref: int | None
    content_md: str | None


@dataclass
class TaskArtifactRef:
    """队友产物或需求文档。``expected_sha256`` 非空时必须与对象字节一致。"""

    name: str | None = None
    oss_ref: str | None = None
    expected_sha256: str | None = None


@dataclass
class TeammateOutput:
    """前序派发的结论和可交给下一位的产物。"""

    role_name: str | None = None
    agent_id: str | None = None
    dispatch_id: str | None = None
    conclusion_md: str | None = None
    artifacts: list[TaskArtifactRef] | None = None


@dataclass
class PackageContext:
    """一次打包的全部输入。打包器只序列化，不回库。"""

    tenant_id: int | None = None
    dispatch_id: int | None = None
    source_dispatch_id: int | None = None
    workitem_id: int | None = None
    agent_id: int | None = None
    sdlc_step_id: int | None = None
    workitem_title: str | None = None
    workitem_content_md: str | None = None
    clarification_md: str | None = None
    comments_md: str | None = None
    comments: list[TaskComment] | None = None
    interaction_context_md: str | None = None
    identity: dict[str, object] | None = None
    repos: list[dict[str, object]] | None = None
    repo_map: dict[str, object] | None = None
    skills: list[dict[str, object]] | None = None
    memory: dict[str, str] | None = None
    sdlc: dict[str, object] | None = None
    omit_sdlc_file_when_absent: bool = False
    teammates: list[TeammateOutput] | None = None
    source_revision_artifacts: list[TaskArtifactRef] | None = None
    requirement_documents: list[TaskArtifactRef] | None = None
    attempt: int | None = None
    work_type: str | None = None
    task_pattern_key: str | None = None
    session_role: str | None = None
    trial_id: str | None = None
    trial_arm: str | None = None
    sdlc_id: int | None = None
    agent_version_id: int | None = None
    executor_id: int | None = None
    role_code: str | None = None
    role_name: str | None = None
    idempotency_key: str | None = None
    roster: dict[str, object] | None = None
    workitem_status: dict[str, object] | None = None


@dataclass
class TaskPackageResult:
    """上传后的包引用。``sha256`` 是 zip 的十六进制摘要，不含 ``sha256:`` 前缀。"""

    oss_ref: str
    md5: str
    size: int
    download_url: str
    sha256: str
    content_hash: str
    issuer: str | None = None
    signature_ref: str | None = None
    signature: str | None = None
    signature_algorithm: str | None = None
    signature_public_key: str | None = None
    expires_at: str | None = None
    allow_commit: bool = False
    allow_push: bool = False
    allow_network: bool = False
    requires_hook_protocol: bool = False
    requires_tool_hook_protocol: bool = False
    mcp_secret_refs: dict[str, str] = field(default_factory=dict)
