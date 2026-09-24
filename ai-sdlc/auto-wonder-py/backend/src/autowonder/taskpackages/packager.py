"""把 ``PackageContext`` 打成 zip，上传并签发下载地址。

布局对齐 ``TaskPackager``：清单 ``autoWonder.taskPackage.v1``，
文件摘要在写入 ``manifest.json`` 之前冻结。签名器缺省为空。
"""

import io
import json
import logging
import re
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import yaml

from autowonder.core.errors import BizError, ErrorCode
from autowonder.scheduledtasks.trigger import java_instant
from autowonder.storage.objects import ObjectStorage, sha256_hex
from autowonder.taskpackages.context import PackageContext, TaskComment, TaskPackageResult

logger = logging.getLogger(__name__)

DOWNLOAD_TTL_SECONDS = 600
_CHECKPOINT_SOURCE_REVISION_SCHEMA = "autowonder.checkpointSourceRevision.v1"
_CHECKPOINT_FALLBACK = "checkpointFallback"
_ARCHIVE_LIMIT = 50 * 1024 * 1024
_MAX_ENTRIES = 500
_CAPABILITY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CAPABILITY_ID = re.compile(r"[A-Za-z0-9._-]+")
_DRIVE = re.compile(r"^[A-Za-z]:.*")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_FORBIDDEN_BRANCH = set("~^:?*[\\")


def java_json(value: object) -> str:
    """紧凑 JSON。对象里的 null 不写出，与 Fastjson 缺省一致。"""
    return json.dumps(_omit_null(value), ensure_ascii=False, separators=(",", ":"))


def normalize_base_url(public_base_url: str | None) -> str:
    """公网地址必须是没有查询和片段的绝对 http(s) URL。"""
    configured = "" if public_base_url is None else public_base_url.strip()
    if configured == "":
        raise RuntimeError("autowonder.public-base-url must be configured")
    parsed = urlsplit(configured)
    scheme = parsed.scheme
    if (
        scheme.lower() not in ("http", "https")
        or parsed.hostname is None
        or parsed.query != ""
        or parsed.fragment != ""
    ):
        raise RuntimeError("autowonder.public-base-url must be an absolute http(s) URL")
    return configured.rstrip("/")


def normalize_mcp_url(mcp_url: str | None) -> str:
    """去掉 MCP 地址末尾的斜杠。空地址不能打包。"""
    configured = "" if mcp_url is None else mcp_url.strip()
    if configured == "":
        raise RuntimeError("autowonder MCP URL must be configured")
    return configured.rstrip("/")


class TaskPackager:
    """序列化、压缩、上传、预签名。"""

    def __init__(
        self,
        storage: ObjectStorage,
        bucket: str,
        mcp_url: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._bucket = bucket
        self._mcp_url = normalize_mcp_url(mcp_url)
        self._clock = clock if clock is not None else _utc_now

    def build(self, ctx: PackageContext) -> TaskPackageResult:
        """打主任务包并上传到 ``{tenant}/{workitem}/{dispatch}.zip``。"""
        logger.info("taskpackage build start dispatchId=%s", ctx.dispatch_id)
        archive = self._assemble_zip(ctx)
        digest = sha256_hex(archive)
        logger.info(
            "taskpackage zip created dispatchId=%s size=%s sha256=%s",
            ctx.dispatch_id,
            len(archive),
            digest,
        )
        key = (
            _text_id(ctx.tenant_id)
            + "/"
            + _text_id(ctx.workitem_id)
            + "/"
            + _text_id(ctx.dispatch_id)
            + ".zip"
        )
        stored = self._storage.put(self._bucket, key, archive)
        logger.info(
            "taskpackage uploaded dispatchId=%s ossRef=%s",
            ctx.dispatch_id,
            stored.oss_ref,
        )
        url = self._storage.presign_get(stored.oss_ref, DOWNLOAD_TTL_SECONDS)
        logger.info(
            "taskpackage download url ready dispatchId=%s ossRef=%s ttlSeconds=%s",
            ctx.dispatch_id,
            stored.oss_ref,
            DOWNLOAD_TTL_SECONDS,
        )
        result = TaskPackageResult(
            oss_ref=stored.oss_ref,
            md5=stored.md5,
            size=stored.size,
            download_url=url,
            sha256=digest,
            content_hash=digest,
            allow_commit=_aggregate_policy(ctx, "allowCommit"),
            allow_push=_aggregate_policy(ctx, "allowPush"),
            allow_network=_aggregate_policy(ctx, "allowNetwork"),
            requires_hook_protocol=_has_capability_type(ctx, "HOOK"),
            requires_tool_hook_protocol=self._has_tool_hook(ctx),
        )
        result.mcp_secret_refs = _collect_mcp_secret_refs(ctx)
        return result

    def build_conversation_capabilities(
        self,
        tenant_id: int,
        conversation_id: int,
        turn_id: int,
        agent_id: int,
        agent_version_id: int,
        capabilities: list[dict[str, object]],
        repos: list[dict[str, object]] | None = None,
        repo_map: dict[str, object] | None = None,
    ) -> TaskPackageResult:
        """会话轮次只打包能力、仓库和清单。"""
        ctx = PackageContext(
            tenant_id=tenant_id,
            workitem_id=conversation_id,
            dispatch_id=turn_id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            skills=capabilities,
            repos=repos if repos is not None else [],
            repo_map=repo_map,
        )
        archive, content_hash = self._conversation_zip(ctx, conversation_id, turn_id)
        digest = sha256_hex(archive)
        key = (
            str(tenant_id)
            + "/conversations/"
            + str(conversation_id)
            + "/turns/"
            + str(turn_id)
            + "-v"
            + str(agent_version_id)
            + ".zip"
        )
        stored = self._storage.put(self._bucket, key, archive)
        url = self._storage.presign_get(stored.oss_ref, DOWNLOAD_TTL_SECONDS)
        result = TaskPackageResult(
            oss_ref=stored.oss_ref,
            md5=stored.md5,
            size=stored.size,
            download_url=url,
            sha256=digest,
            content_hash=content_hash,
        )
        result.mcp_secret_refs = _collect_mcp_secret_refs(ctx)
        return result

    def _assemble_zip(self, ctx: PackageContext) -> bytes:
        buffer = io.BytesIO()
        digests: dict[str, str] = {}
        try:
            with zipfile.ZipFile(buffer, "w") as archive:
                teammates = self._write_teammates(archive, ctx, digests)
                documents = self._write_requirements(archive, ctx, digests)
                _put_text(
                    archive,
                    "workitem.md",
                    _nz(ctx.workitem_title) + "\n\n" + _nz(ctx.workitem_content_md),
                    digests,
                )
                if _present(ctx.clarification_md):
                    _put_text(archive, "clarification.md", ctx.clarification_md or "", digests)
                if _present(ctx.comments_md):
                    _put_text(archive, "comments.md", ctx.comments_md or "", digests)
                comment_index = _write_comment_index(archive, ctx.comments, digests)
                if _present(ctx.interaction_context_md):
                    _put_text(
                        archive,
                        "interaction-context.md",
                        ctx.interaction_context_md or "",
                        digests,
                    )
                _put_text(archive, "identity.json", java_json(_or_empty(ctx.identity)), digests)
                _put_text(
                    archive,
                    "repos.json",
                    java_json({"repos": self._resolve_repos(ctx)}),
                    digests,
                )
                policy = {
                    "allowCommit": _aggregate_policy(ctx, "allowCommit"),
                    "allowPush": _aggregate_policy(ctx, "allowPush"),
                    "allowNetwork": _aggregate_policy(ctx, "allowNetwork"),
                }
                _put_text(archive, "policy.json", java_json(policy), digests)
                if ctx.repo_map is not None:
                    _put_text(archive, "repo-map.json", java_json(ctx.repo_map), digests)
                hooks: list[dict[str, object]] = []
                skills = self._write_capabilities(archive, ctx, digests, hooks)
                _put_text(archive, "skills.json", java_json(skills), digests)
                if hooks:
                    hook_document = {"schemaVersion": "autowonder.hooks.v1", "hooks": hooks}
                    _put_text(archive, "hooks.json", java_json(hook_document), digests)
                if ctx.sdlc is not None or not ctx.omit_sdlc_file_when_absent:
                    _put_text(archive, "sdlc.json", java_json(_or_empty(ctx.sdlc)), digests)
                if ctx.roster is not None:
                    _put_text(archive, "roster.json", java_json(ctx.roster), digests)
                if ctx.workitem_status:
                    _put_text(
                        archive,
                        "workitem-status.json",
                        java_json(ctx.workitem_status),
                        digests,
                    )
                if ctx.memory is not None:
                    for key, content in ctx.memory.items():
                        _put_text(archive, "memory/" + key + ".md", _nz(content), digests)
                manifest: dict[str, object] = {
                    "schemaVersion": "autoWonder.taskPackage.v1",
                    "packageId": "pkg_" + _text_id(ctx.dispatch_id),
                    "capabilityTreeDigest": _tree_digest(digests),
                    "tenantId": _text_id(ctx.tenant_id),
                    "workitemId": _text_id(ctx.workitem_id),
                    "workType": _nz(ctx.work_type),
                    "taskPatternKey": _nz(ctx.task_pattern_key),
                    "sessionRole": _nz(ctx.session_role),
                    "trialId": _nz(ctx.trial_id),
                    "trialArm": _nz(ctx.trial_arm),
                    "dispatchId": _text_id(ctx.dispatch_id),
                    "sourceDispatchId": _text_id(ctx.source_dispatch_id),
                    "attempt": 1 if ctx.attempt is None else ctx.attempt,
                    "idempotencyKey": _nz(ctx.idempotency_key),
                    "sdlcId": _text_id(ctx.sdlc_id),
                    "sdlcStepId": _text_id(ctx.sdlc_step_id),
                    "agentId": _text_id(ctx.agent_id),
                    "agentVersionId": _text_id(ctx.agent_version_id),
                    "executorId": _text_id(ctx.executor_id),
                    "roleCode": _nz(ctx.role_code),
                    "roleName": _nz(ctx.role_name),
                    "createdAt": java_instant(self._clock()),
                    "fileDigests": dict(digests),
                    "teammates": teammates,
                    "requirementDocuments": documents,
                }
                if comment_index is not None:
                    manifest["commentIndex"] = comment_index
                _put_text(archive, "manifest.json", java_json(manifest), digests)
        except BizError:
            raise
        except Exception as error:
            raise BizError(ErrorCode.PACKAGE_BUILD_FAILED) from error
        return buffer.getvalue()

    def _conversation_zip(
        self, ctx: PackageContext, conversation_id: int, turn_id: int
    ) -> tuple[bytes, str]:
        buffer = io.BytesIO()
        digests: dict[str, str] = {}
        try:
            with zipfile.ZipFile(buffer, "w") as archive:
                skills = self._write_capabilities(archive, ctx, digests, [])
                _put_text(archive, "skills.json", java_json(skills), digests)
                if ctx.repos:
                    _put_text(archive, "repos.json", java_json(ctx.repos), digests)
                if ctx.repo_map is not None:
                    _put_text(archive, "repo-map.json", java_json(ctx.repo_map), digests)
                ordered = dict(sorted(digests.items()))
                content_hash = sha256_hex(java_json(ordered).encode())
                manifest = {
                    "schemaVersion": "autoWonder.taskPackage.v1",
                    "packageId": "conversation-capabilities:"
                    + str(conversation_id)
                    + ":"
                    + str(turn_id),
                    "tenantId": _text_id(ctx.tenant_id),
                    "workitemId": "conversation:" + str(conversation_id),
                    "dispatchId": "conversation-turn:" + str(turn_id),
                    "attempt": 1,
                    "agentId": _text_id(ctx.agent_id),
                    "agentVersionId": _text_id(ctx.agent_version_id),
                    "createdAt": java_instant(self._clock()),
                    "fileDigests": dict(digests),
                }
                _put_text(archive, "manifest.json", java_json(manifest), digests)
        except BizError:
            raise
        except Exception as error:
            raise BizError(ErrorCode.PACKAGE_BUILD_FAILED) from error
        return buffer.getvalue(), content_hash

    def _has_tool_hook(self, ctx: PackageContext) -> bool:
        skills = ctx.skills if ctx.skills is not None else []
        for capability in skills:
            if _string_value(capability.get("type")).upper() != "HOOK":
                continue
            oss_ref = _string_value(capability.get("packageOssRef"))
            archive = None if oss_ref == "" else self._storage.get(oss_ref)
            if archive is None:
                raise ValueError("hook package is unavailable: " + _required_name(capability))
            parsed = yaml.safe_load(_root_hook_yaml(archive))
            if not isinstance(parsed, dict):
                raise ValueError("hook descriptor is invalid")
            trigger = _string_value(parsed.get("trigger"))
            if trigger == "beforeTool" or trigger == "afterTool":
                return True
        return False

    def _write_capabilities(
        self,
        archive: zipfile.ZipFile,
        ctx: PackageContext,
        digests: dict[str, str],
        hooks: list[dict[str, object]],
    ) -> dict[str, object]:
        skills: list[dict[str, object]] = []
        plugins: list[dict[str, object]] = []
        mcp_servers = [_builtin_mcp(self._mcp_url)]
        names = {"MCP:autowonder"}
        capabilities = ctx.skills if ctx.skills is not None else []
        for capability in capabilities:
            kind = _required_string(capability, "type").upper()
            name = _required_name(capability)
            if kind == "MCP" and name.lower() == "autowonder":
                continue
            token = kind + ":" + name
            if token in names:
                raise ValueError("duplicate capability " + token)
            names.add(token)
            if kind == "MCP":
                mcp_servers.append(_mcp_descriptor(capability, name))
                continue
            if kind == "HOOK":
                self._write_hook(archive, capability, name, digests, hooks)
                continue
            if kind != "SKILL" and kind != "PLUGIN":
                raise ValueError("unsupported capability type " + kind)
            folder = "skills/" if kind == "SKILL" else "plugins/"
            base = "capabilities/" + folder + name
            descriptor: dict[str, object] = {
                "id": _java_string(capability.get("id")),
                "name": name,
                "path": base,
                "version": _java_string(capability.get("version", 0)),
                "required": capability.get("required") is not False,
            }
            oss_ref = _string_value(capability.get("packageOssRef"))
            if oss_ref == "":
                if kind != "SKILL":
                    raise ValueError("plugin package is required: " + name)
                generated = _generated_skill(capability, name)
                _put_bytes(archive, base + "/SKILL.md", generated, digests)
                descriptor["sha256"] = sha256_hex(generated)
            else:
                packed = self._storage.get(oss_ref)
                if packed is None:
                    if kind == "SKILL" and not self._storage.exists(oss_ref):
                        logger.warning(
                            "skip deleted skill package capabilityId=%s name=%s ossRef=%s",
                            capability.get("id"),
                            name,
                            oss_ref,
                        )
                        continue
                    raise ValueError("capability package is unavailable: " + name)
                _extract_capability(archive, packed, base, digests, kind == "SKILL")
                descriptor["sha256"] = sha256_hex(packed)
            if kind == "PLUGIN":
                config = _config(capability)
                providers = config.get("providers")
                if not isinstance(providers, list | tuple | set) or len(providers) == 0:
                    raise ValueError("plugin providers are required: " + name)
                descriptor["providers"] = providers
                plugins.append(descriptor)
            else:
                skills.append(descriptor)
        return {
            "schemaVersion": "autowonder.capabilities.v1",
            "skills": skills,
            "mcpServers": mcp_servers,
            "plugins": plugins,
        }

    def _write_hook(
        self,
        archive: zipfile.ZipFile,
        capability: dict[str, object],
        name: str,
        digests: dict[str, str],
        hooks: list[dict[str, object]],
    ) -> None:
        oss_ref = _string_value(capability.get("packageOssRef"))
        if oss_ref == "":
            raise ValueError("hook package is required: " + name)
        packed = self._storage.get(oss_ref)
        if packed is None:
            raise ValueError("hook package is unavailable: " + name)
        version = _java_string(capability.get("version", 0))
        digest = _extract_hook(archive, packed, "capabilities/hooks/" + name, digests)
        hooks.append(
            {
                "name": name,
                "version": version,
                "sha256": digest,
                "enabled": capability.get("required") is not False,
            }
        )

    def _write_teammates(
        self,
        archive: zipfile.ZipFile,
        ctx: PackageContext,
        digests: dict[str, str],
    ) -> list[dict[str, object]]:
        manifest: list[dict[str, object]] = []
        if not ctx.teammates:
            return manifest
        used: set[str] = set()
        for teammate in ctx.teammates:
            base = teammate.role_name if _present(teammate.role_name) else "unknown"
            directory = base or "unknown"
            if directory in used:
                directory = (base or "unknown") + "__" + _last6(teammate.dispatch_id)
            used.add(directory)
            _put_text(
                archive,
                "teammates/" + directory + "/conclusion.md",
                _nz(teammate.conclusion_md),
                digests,
            )
            if teammate.artifacts is not None:
                for ref in teammate.artifacts:
                    if ref.oss_ref is None:
                        continue
                    payload = self._storage.get(ref.oss_ref)
                    if payload is None or ref.name is None:
                        continue
                    _put_bytes(
                        archive,
                        "teammates/" + directory + "/artifacts/" + ref.name,
                        payload,
                        digests,
                    )
            manifest.append(
                {
                    "roleName": teammate.role_name,
                    "agentId": teammate.agent_id,
                    "dispatchId": teammate.dispatch_id,
                    "dir": directory,
                }
            )
        return manifest

    def _write_requirements(
        self,
        archive: zipfile.ZipFile,
        ctx: PackageContext,
        digests: dict[str, str],
    ) -> list[dict[str, object]]:
        manifest: list[dict[str, object]] = []
        if not ctx.requirement_documents:
            return manifest
        written: set[str] = set()
        for ref in ctx.requirement_documents:
            entry_name = _requirement_entry_name(ref.name)
            if entry_name in written:
                raise ValueError("duplicate requirement document " + entry_name)
            written.add(entry_name)
            if ref.oss_ref is None:
                raise ValueError("requirement document is unavailable: " + _nz(ref.name))
            payload = self._storage.get(ref.oss_ref)
            if payload is None:
                logger.error(
                    "requirement document missing dispatchId=%s workitemId=%s name=%s ossRef=%s",
                    ctx.dispatch_id,
                    ctx.workitem_id,
                    ref.name,
                    ref.oss_ref,
                )
                raise ValueError("requirement document is unavailable: " + _nz(ref.name))
            actual = "sha256:" + sha256_hex(payload)
            expected = ref.expected_sha256
            if expected is not None and expected.lower() != actual.lower():
                raise ValueError("requirement document digest mismatch: " + _nz(ref.name))
            _put_bytes(archive, entry_name, payload, digests)
            manifest.append({"name": entry_name, "size": len(payload), "sha256": actual})
        return manifest

    def _resolve_repos(self, ctx: PackageContext) -> list[dict[str, object]]:
        repos = [dict(repo) for repo in ctx.repos] if ctx.repos is not None else []
        revisions = self._load_source_revisions(ctx)
        for repo in repos:
            revision = revisions.get(str(repo.get("name")))
            if revision is None or not _valid_commit(revision.get("headCommit")):
                continue
            repo["ref"] = revision["headCommit"]
            if not _as_bool(revision.get(_CHECKPOINT_FALLBACK)) and _valid_commit(
                revision.get("baseCommit")
            ):
                repo["deliveryBaseCommit"] = revision["baseCommit"]
            if _valid_branch(revision.get("branch")):
                repo["deliveryBranch"] = revision["branch"]
        return repos

    def _load_source_revisions(self, ctx: PackageContext) -> dict[str, dict[str, str]]:
        revisions: dict[str, dict[str, str]] = {}
        refs = list(ctx.source_revision_artifacts or [])
        if not refs and ctx.teammates:
            for teammate in ctx.teammates:
                if teammate.artifacts:
                    refs.extend(teammate.artifacts)
        for ref in refs:
            name = "" if ref.name is None else ref.name.replace("\\", "/")
            if not name.endswith("deliverables/runtime-source-revision.json"):
                continue
            if ref.oss_ref is None:
                continue
            raw = self._storage.get(ref.oss_ref)
            if raw is None:
                continue
            _merge_revision(revisions, name, raw)
        return revisions


def _merge_revision(revisions: dict[str, dict[str, str]], name: str, raw: bytes) -> None:
    try:
        document = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("invalid runtime source revision ignored artifact=%s", name)
        return
    if not isinstance(document, dict):
        logger.warning("invalid runtime source revision ignored artifact=%s", name)
        return
    checkpoint_fallback = document.get("schemaVersion") == _CHECKPOINT_SOURCE_REVISION_SCHEMA
    repositories = document.get("repositories")
    if not isinstance(repositories, list):
        return
    for revision in repositories:
        if not isinstance(revision, dict):
            continue
        repo_name = revision.get("name")
        if not isinstance(repo_name, str) or repo_name.strip() == "":
            continue
        values = revisions.setdefault(repo_name, {})
        head = revision.get("headCommit")
        if (
            isinstance(head, str)
            and not _valid_commit(values.get("headCommit"))
            and _valid_commit(head)
        ):
            values["headCommit"] = head
            values[_CHECKPOINT_FALLBACK] = "true" if checkpoint_fallback else "false"
        base = revision.get("baseCommit")
        if (
            isinstance(base, str)
            and not _as_bool(values.get(_CHECKPOINT_FALLBACK))
            and not _valid_commit(values.get("baseCommit"))
            and _valid_commit(base)
        ):
            values["baseCommit"] = base
        branch = revision.get("branch")
        if (
            isinstance(branch, str)
            and not _valid_branch(values.get("branch"))
            and _valid_branch(branch)
        ):
            values["branch"] = branch


def _write_comment_index(
    archive: zipfile.ZipFile,
    comments: list[TaskComment] | None,
    digests: dict[str, str],
) -> str | None:
    if not comments:
        return None
    entries: list[dict[str, object]] = []
    seen: set[int] = set()
    for comment in comments:
        if comment.id <= 0 or comment.id in seen:
            raise ValueError("comment snapshot must have a unique positive id")
        seen.add(comment.id)
        path = "context/comments/" + str(comment.id) + ".md"
        content = _nz(comment.content_md).encode()
        _put_bytes(archive, path, content, digests)
        entries.append(
            {
                "id": str(comment.id),
                "authorType": _nz(comment.author_type),
                "authorRef": _text_id(comment.author_ref),
                "path": path,
                "sha256": digests[path],
                "sizeBytes": len(content),
            }
        )
    path = "context/comments-index.json"
    index = {"schemaVersion": "autowonder.commentIndex.v1", "comments": entries}
    _put_text(archive, path, java_json(index), digests)
    return path


def _extract_capability(
    archive: zipfile.ZipFile,
    packed: bytes,
    base: str,
    digests: dict[str, str],
    require_skill_md: bool,
) -> None:
    files = _zip_files(packed, "capability")
    has_skill = False
    for name, payload in files:
        if name == "SKILL.md":
            has_skill = True
        _put_bytes(archive, base + "/" + name, payload, digests)
    if len(files) == 0 or (require_skill_md and not has_skill):
        if require_skill_md:
            raise ValueError("skill package must contain root SKILL.md")
        raise ValueError("plugin package is empty")


def _extract_hook(
    archive: zipfile.ZipFile,
    packed: bytes,
    base: str,
    digests: dict[str, str],
) -> str:
    files = _zip_files(packed, "hook")
    has_descriptor = False
    hook_digests: dict[str, str] = {}
    for name, payload in files:
        if name == "hook.yaml":
            has_descriptor = True
        hook_digests[name] = "sha256:" + sha256_hex(payload)
        _put_bytes(archive, base + "/" + name, payload, digests)
    if len(files) == 0 or not has_descriptor:
        raise ValueError("hook package must contain root hook.yaml")
    return _paired_digest(hook_digests)


def _root_hook_yaml(archive: bytes) -> str:
    for name, payload in _zip_files(archive, "hook"):
        if name == "hook.yaml":
            return payload.decode()
    raise ValueError("hook package must contain root hook.yaml")


def _zip_files(archive: bytes, label: str) -> list[tuple[str, bytes]]:
    expanded = 0
    entries = 0
    files: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for info in source.infolist():
            entries += 1
            if entries > _MAX_ENTRIES:
                raise ValueError(label + " package has too many entries")
            raw_name = info.filename
            if info.is_dir() and raw_name.endswith("/"):
                raw_name = raw_name[:-1]
            name = safe_archive_path(raw_name)
            if info.is_dir():
                continue
            payload = source.read(info)
            expanded += len(payload)
            if expanded > _ARCHIVE_LIMIT:
                raise ValueError(label + " package is too large")
            files.append((name, payload))
    return files


def safe_archive_path(raw: str | None) -> str:
    """拒绝绝对路径、盘符和 ``..``。ZIP 名是逻辑路径。"""
    if (
        raw is None
        or raw.strip() == ""
        or raw.startswith("/")
        or raw.startswith("\\")
        or "\\" in raw
    ):
        raise ValueError("unsafe capability archive path")
    if "\0" in raw or _DRIVE.fullmatch(raw) is not None:
        raise ValueError("unsafe capability archive path " + raw)
    for segment in raw.split("/"):
        if segment == "" or segment == "." or segment == "..":
            raise ValueError("unsafe capability archive path " + raw)
    return raw


def _required_name(capability: dict[str, object]) -> str:
    name = _required_string(capability, "name")
    if _CAPABILITY_NAME.fullmatch(name) is not None:
        return name
    identifier = _required_string(capability, "id")
    if _CAPABILITY_ID.fullmatch(identifier) is None:
        raise ValueError("invalid capability id " + identifier)
    return "capability-" + identifier


def _required_string(value: dict[str, object], key: str) -> str:
    text = _string_value(value.get(key))
    if text == "":
        raise ValueError("capability " + key + " is required")
    return text


def _string_value(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _generated_skill(capability: dict[str, object], name: str) -> bytes:
    description = _string_value(capability.get("description"))
    raw_config = capability.get("config")
    instructions = ""
    if isinstance(raw_config, dict):
        instructions = _string_value(raw_config.get("instructions"))
    if instructions == "":
        instructions = description
    shown = name if description == "" else description.replace("\n", " ")
    markdown = (
        "---\nname: "
        + _yaml_string(name)
        + "\ndescription: "
        + _yaml_string(shown)
        + "\n---\n\n# "
        + name
        + "\n\n"
        + instructions
        + "\n"
    )
    return markdown.encode()


def _yaml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return '"' + escaped + '"'


def _builtin_mcp(mcp_url: str) -> dict[str, object]:
    return {
        "name": "autowonder",
        "transport": "http",
        "url": mcp_url,
        "authType": "autowonder-dispatch",
        "required": True,
    }


def _mcp_descriptor(capability: dict[str, object], name: str) -> dict[str, object]:
    descriptor = _config(capability)
    _split_secret_refs(descriptor, "headers", "headerSecretRefs")
    _split_secret_refs(descriptor, "env", "envSecretRefs")
    descriptor["name"] = name
    descriptor["required"] = capability.get("required") is not False
    return descriptor


def _split_secret_refs(descriptor: dict[str, object], key: str, refs_key: str) -> None:
    raw = descriptor.get(key)
    if not isinstance(raw, dict):
        return
    literals: dict[str, object] = {}
    refs: dict[str, str] = {}
    for name, value in raw.items():
        if isinstance(value, dict) and str(value.get("kind")) == "secretRef":
            ref = value.get("ref")
            if ref is None or str(ref).strip() == "":
                raise ValueError("MCP " + key + " secret reference is missing: " + str(name))
            refs[str(name)] = str(ref)
        else:
            literals[str(name)] = value
    descriptor[key] = literals
    if refs:
        descriptor[refs_key] = refs


def _collect_mcp_secret_refs(ctx: PackageContext) -> dict[str, str]:
    refs: dict[str, str] = {}
    for capability in ctx.skills or []:
        if _string_value(capability.get("type")).upper() != "MCP":
            continue
        config = _config(capability)
        _collect_secret_refs(config.get("headers"), refs)
        _collect_secret_refs(config.get("env"), refs)
    return refs


def _collect_secret_refs(raw: object, refs: dict[str, str]) -> None:
    if not isinstance(raw, dict):
        return
    for value in raw.values():
        if isinstance(value, dict) and str(value.get("kind")) == "secretRef":
            ref = value.get("ref")
            if ref is not None and str(ref).strip() != "":
                refs[str(ref)] = str(ref)


def _config(capability: dict[str, object]) -> dict[str, object]:
    value = capability.get("config")
    if not isinstance(value, dict):
        raise ValueError("capability config is required: " + str(capability.get("name")))
    return dict(value)


def _tree_digest(digests: dict[str, str]) -> str:
    selected = {
        name: digest
        for name, digest in digests.items()
        if name in ("skills.json", "hooks.json") or name.startswith("capabilities/")
    }
    return _paired_digest(selected)


def _paired_digest(pairs: dict[str, str]) -> str:
    material = bytearray()
    for name in sorted(pairs):
        material.extend(name.encode())
        material.append(0)
        material.extend(pairs[name].encode())
        material.append(ord("\n"))
    return "sha256:" + sha256_hex(bytes(material))


def _requirement_entry_name(raw_name: str | None) -> str:
    normalized = safe_archive_path("" if raw_name is None else raw_name.replace("\\", "/"))
    filename = normalized
    if normalized.startswith("requirements/"):
        filename = normalized[len("requirements/") :]
    if filename.strip() == "" or "/" in filename:
        raise ValueError("unsafe requirement document path " + str(raw_name))
    return "requirements/" + filename


def _aggregate_policy(ctx: PackageContext, field_name: str) -> bool:
    if ctx.repos is None:
        return False
    return any(repo.get(field_name) is True for repo in ctx.repos)


def _has_capability_type(ctx: PackageContext, expected: str) -> bool:
    if ctx.skills is None:
        return False
    return any(_string_value(item.get("type")).upper() == expected.upper() for item in ctx.skills)


def _valid_commit(commit: str | None) -> bool:
    if commit is None:
        return False
    return _COMMIT.fullmatch(commit) is not None


def _valid_branch(branch: str | None) -> bool:
    if branch is None or branch.strip() == "" or len(branch) > 255:
        return False
    if (
        branch.startswith("-")
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.endswith(".")
    ):
        return False
    if (
        branch.endswith(".lock")
        or branch == "@"
        or ".." in branch
        or "@{" in branch
        or "//" in branch
    ):
        return False
    for char in branch:
        if ord(char) < 32 or char.isspace() or char in _FORBIDDEN_BRANCH:
            return False
    for part in branch.split("/"):
        if part == "" or part.startswith(".") or part.endswith(".lock"):
            return False
    return True


def _as_bool(value: str | None) -> bool:
    return value == "true"


def _last6(identifier: str | None) -> str:
    text = "0" if identifier is None or identifier.strip() == "" else identifier
    if len(text) <= 6:
        return text
    return text[-6:]


def _put_text(archive: zipfile.ZipFile, name: str, content: str, digests: dict[str, str]) -> None:
    _put_bytes(archive, name, content.encode(), digests)


def _put_bytes(
    archive: zipfile.ZipFile, name: str, payload: bytes, digests: dict[str, str]
) -> None:
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, payload)
    if name != "manifest.json":
        digests[name] = "sha256:" + sha256_hex(payload)


def _text_id(value: int | None) -> str:
    if value is None:
        return ""
    return str(value)


def _nz(value: str | None) -> str:
    if value is None:
        return ""
    return value


def _present(value: str | None) -> bool:
    return value is not None and value.strip() != ""


def _or_empty(value: dict[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    return value


def _java_string(value: object) -> str:
    if value is None:
        return "null"
    return str(value)


def _omit_null(value: object) -> object:
    if isinstance(value, dict):
        return {key: _omit_null(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_null(item) for item in value]
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)

