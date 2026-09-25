"""需求文档的白名单、配额、对象路径和审计。工单与定时任务共用这一套。"""

import asyncio
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import Any, TypeGuard, cast

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from autowonder.artifacts.models import Artifact
from autowonder.artifacts.schemas import ArtifactView
from autowonder.audits.service import AuditRecord, record_required
from autowonder.config import get_settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.query import execution_source_type
from autowonder.scheduledtasks.models import ScheduledTask
from autowonder.storage.objects import ObjectStorageError, get_object_storage
from autowonder.workitems.models import Workitem

TYPE = "REQUIREMENT_DOC"
PREFIX = "requirements/"
CLARIFICATION_FILENAME = "clarification.md"
MAX_DOCUMENTS = 10
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ZIP_ENTRIES = 500
MAX_ZIP_INFLATED_BYTES = 50 * 1024 * 1024
MAX_ZIP_PATH_DEPTH = 20
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_REQUIRED_ENTRY = "word/document.xml"
_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")
_MUTATION = asyncio.Lock()

_MARKDOWN = ("text/markdown", "MARKDOWN")
_TEXT = ("text/plain", "TEXT")
_HTML = ("text/html", "TEXT")
_PDF = ("application/pdf", "PDF")
_PNG = ("image/png", "VISUAL")
_JPEG = ("image/jpeg", "VISUAL")
_WEBP = ("image/webp", "VISUAL")
_DOCX = (DOCX_CONTENT_TYPE, "WORD")
_DOC = ("application/msword", "WORD")
_JAVA = ("text/x-java-source", "CODE")
_PYTHON = ("text/x-python", "CODE")
_ZIP = ("application/zip", "ARCHIVE")
_TYPES = {
    "md": _MARKDOWN,
    "markdown": _MARKDOWN,
    "txt": _TEXT,
    "html": _HTML,
    "pdf": _PDF,
    "png": _PNG,
    "jpg": _JPEG,
    "jpeg": _JPEG,
    "webp": _WEBP,
    "docx": _DOCX,
    "doc": _DOC,
    "java": _JAVA,
    "py": _PYTHON,
    "zip": _ZIP,
}
SUPPORTED_EXTENSIONS = tuple("." + extension for extension in _TYPES)


@dataclass(frozen=True)
class ArtifactOwner:
    """产物归属。定时任务的 source_id 写在 artifact.workitem_id 列上。"""

    source_type: str
    source_id: int


@dataclass(frozen=True)
class DocumentCandidate:
    """一份待写入的需求文档。"""

    filename: str
    payload: bytes
    source_path: str | None
    content_type: str
    context_kind: str


@dataclass(frozen=True)
class DocumentContent:
    """CLI 下载用的文件名、正文和内容类型。"""

    filename: str
    payload: bytes
    content_type: str


def requirement_bucket() -> str:
    """产物桶有文本时用产物桶，否则用默认桶。"""
    settings = get_settings()
    if _has_text(settings.oss_artifact_bucket):
        return settings.oss_artifact_bucket
    return settings.oss_bucket


def workitem_owner(workitem_id: int) -> ArtifactOwner:
    """工单归属。"""
    return ArtifactOwner("WORKITEM", workitem_id)


def sanitize_filename(raw: str | None) -> str:
    """去掉 Java trim 的空白。路径片段和空名拒绝。"""
    if raw is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    filename = _java_trim(raw)
    if filename == "" or "/" in filename or "\\" in filename:
        raise BizError(ErrorCode.PARAM_INVALID)
    if filename == "." or filename == ".." or ".." in filename:
        raise BizError(ErrorCode.PARAM_INVALID)
    for char in filename:
        if _iso_control(char):
            raise BizError(ErrorCode.PARAM_INVALID)
    return filename


def extension_of(filename: str) -> str:
    """最后一个点之后的小写后缀。点在末尾时没有后缀。"""
    lower = filename.lower()
    dot = lower.rfind(".")
    if dot < 0 or dot == len(lower) - 1:
        return ""
    return lower[dot + 1 :]


def file_type_for(filename: str) -> tuple[str, str]:
    """扩展名对应的内容类型和上下文种类。"""
    found = _TYPES.get(extension_of(filename))
    if found is None:
        supported = "、".join(SUPPORTED_EXTENSIONS)
        raise BizError(ErrorCode.PARAM_INVALID, "不支持的文件格式，仅支持 " + supported)
    return found


def validate_bytes(payload: bytes | None, content_type: str, kind: str) -> None:
    """按种类核对正文。超过单文件上限时不看内容。"""
    if payload is None or len(payload) > MAX_FILE_BYTES:
        raise BizError(ErrorCode.PARAM_INVALID)
    if kind == "MARKDOWN" or kind == "TEXT" or kind == "CODE":
        _validate_text(payload)
        return
    if kind == "PDF":
        _require_signature(_has_pdf_signature(payload), "文件内容与 PDF 格式不符")
        return
    if kind == "VISUAL":
        _require_signature(_has_image_signature(content_type, payload), "文件内容与图片格式不符")
        return
    if kind == "WORD":
        _validate_word(payload, content_type)
        return
    if kind == "ARCHIVE":
        _validate_zip(payload, None)
        return
    raise BizError(ErrorCode.PARAM_INVALID)


def validate_limits(existing: list[Artifact], candidates: list[DocumentCandidate]) -> None:
    """总数不超过 10，合计不超过 20MB，同名冲突。"""
    names: set[str] = set()
    total = 0
    for row in existing:
        names.add(row.name)
        if row.size is not None:
            total += row.size
    if len(existing) + len(candidates) > MAX_DOCUMENTS:
        raise BizError(ErrorCode.PARAM_INVALID)
    for candidate in candidates:
        name = PREFIX + candidate.filename
        if name in names:
            raise BizError(ErrorCode.CONFLICT)
        names.add(name)
        if len(candidate.payload) > MAX_FILE_BYTES:
            raise BizError(ErrorCode.PARAM_INVALID)
        total += len(candidate.payload)
    if total > MAX_TOTAL_BYTES:
        raise BizError(ErrorCode.PARAM_INVALID)


def meta_payload(
    source: str,
    user_id: int,
    source_path: str | None,
    content_type: str,
    context_kind: str,
) -> dict[str, object]:
    """写入 meta_json 的对象。空来源路径不出现。"""
    meta: dict[str, object] = {
        "source": source,
        "uploaderId": user_id,
        "contentType": content_type,
        "contextKind": context_kind,
    }
    if source_path is not None and not _is_blank(source_path):
        meta["sourcePath"] = source_path
    return meta


def artifact_upsert_statement(
    workspace_id: int,
    owner: ArtifactOwner,
    name: str,
    oss_ref: str,
    size: int,
    meta: dict[str, object],
) -> Any:
    """与 Java insert 相同：冲突时复用原主键并覆盖内容列。"""
    statement = mysql_insert(Artifact).values(
        tenant_id=workspace_id,
        source_type=owner.source_type,
        workitem_id=owner.source_id,
        dispatch_id=None,
        name=name,
        type=TYPE,
        oss_ref=oss_ref,
        size=size,
        meta_json=meta,
    )
    return statement.on_duplicate_key_update(
        id=text("LAST_INSERT_ID(id)"),
        source_type=statement.inserted.source_type,
        workitem_id=statement.inserted.workitem_id,
        type=statement.inserted.type,
        oss_ref=statement.inserted.oss_ref,
        size=statement.inserted.size,
        meta_json=statement.inserted.meta_json,
    )


def list_requirement_statement(workspace_id: int, owner: ArtifactOwner) -> Select[tuple[Artifact]]:
    """需求文档按 id 升序。工单来源限定 WORKITEM。"""
    if owner.source_type == "WORKITEM":
        return (
            select(Artifact)
            .where(
                Artifact.tenant_id == workspace_id,
                Artifact.source_type == "WORKITEM",
                Artifact.workitem_id == owner.source_id,
                Artifact.type == TYPE,
            )
            .order_by(Artifact.id.asc())
        )
    return (
        select(Artifact)
        .where(
            Artifact.tenant_id == workspace_id,
            Artifact.source_type == owner.source_type,
            Artifact.workitem_id == owner.source_id,
            Artifact.type == TYPE,
        )
        .order_by(Artifact.id.asc())
    )


def find_requirement_statement(
    workspace_id: int,
    owner: ArtifactOwner,
    artifact_id: int,
) -> Select[tuple[Artifact]]:
    """工单按租户和主键查。其他来源再限定来源 id。"""
    if owner.source_type == "WORKITEM":
        return (
            select(Artifact)
            .where(
                Artifact.tenant_id == workspace_id,
                Artifact.source_type == "WORKITEM",
                Artifact.id == artifact_id,
            )
            .limit(1)
        )
    return (
        select(Artifact)
        .where(
            Artifact.tenant_id == workspace_id,
            Artifact.source_type == owner.source_type,
            Artifact.workitem_id == owner.source_id,
            Artifact.id == artifact_id,
        )
        .limit(1)
    )


def delete_requirement_statement(workspace_id: int, owner: ArtifactOwner, artifact_id: int) -> Any:
    """删除一行需求文档。范围与查找一致。"""
    if owner.source_type == "WORKITEM":
        return delete(Artifact).where(
            Artifact.tenant_id == workspace_id,
            Artifact.source_type == "WORKITEM",
            Artifact.id == artifact_id,
        )
    return delete(Artifact).where(
        Artifact.tenant_id == workspace_id,
        Artifact.source_type == owner.source_type,
        Artifact.workitem_id == owner.source_id,
        Artifact.id == artifact_id,
    )


def find_workitem_statement(workitem_id: int) -> Select[tuple[Workitem]]:
    """按主键读取未删除工单。租户在读到行之后核对。"""
    return select(Workitem).where(Workitem.id == workitem_id, Workitem.is_deleted == 0).limit(1)


def find_scheduled_task_statement(
    workspace_id: int,
    task_id: int,
    mutation: bool,
) -> Select[tuple[ScheduledTask]]:
    """按工作空间读取未删除任务。修改时锁定该行。"""
    statement = select(ScheduledTask).where(
        ScheduledTask.workspace_id == workspace_id,
        ScheduledTask.id == task_id,
        ScheduledTask.is_deleted == 0,
    )
    if mutation:
        return statement.with_for_update()
    return statement


def requirement_view(row: Artifact) -> ArtifactView:
    """需求文档保持登记类型，不按路径重分类。"""
    return ArtifactView(
        id=row.id,
        workitem_id=row.workitem_id,
        dispatch_id=row.dispatch_id,
        name=row.name,
        type=row.type,
        size=row.size,
        gmt_create=row.gmt_create,
    )


async def list_requirement_documents(
    session: AsyncSession,
    owner: ArtifactOwner,
    workspace_id: int,
) -> list[ArtifactView]:
    """列出归属下的需求文档。归档任务仍可列。"""
    await _ensure_owner(session, owner, workspace_id, False)
    rows = await session.scalars(list_requirement_statement(workspace_id, owner))
    return [requirement_view(row) for row in rows]


async def upload_mcp(
    session: AsyncSession,
    owner: ArtifactOwner,
    filename: str | None,
    payload: bytes | None,
    workspace_id: int,
    user_id: int,
    source_path: str | None,
) -> ArtifactView:
    """MCP 上传一份需求文档。"""
    async with _MUTATION:
        view = await _upload_mcp(
            session,
            owner,
            filename,
            payload,
            workspace_id,
            user_id,
            source_path,
        )
        await session.commit()
        return view


async def upload_named_files(
    session: AsyncSession,
    owner: ArtifactOwner,
    files: list[tuple[str | None, bytes]],
    workspace_id: int,
    user_id: int,
    source: str,
) -> list[ArtifactView]:
    """按提交顺序保存网页或 CLI 上传的多份文件。"""
    async with _MUTATION:
        views = await _upload_named_files(
            session,
            owner,
            files,
            workspace_id,
            user_id,
            source,
        )
        await session.commit()
        return views


async def delete_requirement_document(
    session: AsyncSession,
    owner: ArtifactOwner,
    artifact_id: int,
    workspace_id: int,
    user_id: int,
) -> None:
    """删除需求文档对象和登记行，并写审计。"""
    async with _MUTATION:
        await _delete_requirement_document(session, owner, artifact_id, workspace_id, user_id)
        await session.commit()


async def replace_clarification_document(
    session: AsyncSession,
    workitem_id: int,
    content_md: str | None,
    workspace_id: int,
    user_id: int,
) -> ArtifactView:
    """用新的澄清正文替换已生成的 clarification.md，不额外占用名额。"""
    async with _MUTATION:
        view = await _replace_clarification(
            session,
            workitem_id,
            content_md,
            workspace_id,
            user_id,
        )
        await session.commit()
        return view


async def read_requirement_document(
    session: AsyncSession,
    workitem_id: int,
    artifact_id: int,
    workspace_id: int,
) -> DocumentContent:
    """读取一份工单需求文档的原始文件名和正文。"""
    owner = workitem_owner(workitem_id)
    await _ensure_owner(session, owner, workspace_id, False)
    row = await session.scalar(find_requirement_statement(workspace_id, owner, artifact_id))
    if not _is_requirement(row, workspace_id, owner):
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    stored = row
    payload = get_object_storage().get(stored.oss_ref)
    if payload is None:
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    filename = _strip_prefix(stored.name)
    content_type, _kind = file_type_for(filename)
    return DocumentContent(filename, payload, content_type)


async def _upload_mcp(
    session: AsyncSession,
    owner: ArtifactOwner,
    filename: str | None,
    payload: bytes | None,
    workspace_id: int,
    user_id: int,
    source_path: str | None,
) -> ArtifactView:
    safe = sanitize_filename(filename)
    content_type, kind = file_type_for(safe)
    validate_bytes(payload, content_type, kind)
    candidate = DocumentCandidate(safe, cast(bytes, payload), source_path, content_type, kind)
    views = await _store_all(session, owner, [candidate], workspace_id, user_id, "MCP")
    return views[0]


async def _upload_named_files(
    session: AsyncSession,
    owner: ArtifactOwner,
    files: list[tuple[str | None, bytes]],
    workspace_id: int,
    user_id: int,
    source: str,
) -> list[ArtifactView]:
    if len(files) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    candidates: list[DocumentCandidate] = []
    for filename, payload in files:
        safe = sanitize_filename(filename)
        content_type, kind = file_type_for(safe)
        validate_bytes(payload, content_type, kind)
        candidates.append(DocumentCandidate(safe, payload, None, content_type, kind))
    return await _store_all(session, owner, candidates, workspace_id, user_id, source)


async def _replace_clarification(
    session: AsyncSession,
    workitem_id: int,
    content_md: str | None,
    workspace_id: int,
    user_id: int,
) -> ArtifactView:
    owner = workitem_owner(workitem_id)
    await _ensure_owner(session, owner, workspace_id, True)
    existing = await _load_documents(session, workspace_id, owner)
    for row in existing:
        if row.name == PREFIX + CLARIFICATION_FILENAME:
            get_object_storage().delete(row.oss_ref)
            await session.execute(delete_requirement_statement(workspace_id, owner, row.id))
            await _audit(
                session,
                workspace_id,
                user_id,
                owner,
                row.id,
                row.name,
                row.size,
                "DELETE_REQUIREMENT_DOC",
                "CLARIFICATION",
            )
    if content_md is None:
        payload = b""
    else:
        payload = content_md.encode("utf-8")
    candidate = DocumentCandidate(
        CLARIFICATION_FILENAME,
        payload,
        "autowonder:clarification",
        _MARKDOWN[0],
        _MARKDOWN[1],
    )
    views = await _store_all(session, owner, [candidate], workspace_id, user_id, "CLARIFICATION")
    return views[0]


async def _delete_requirement_document(
    session: AsyncSession,
    owner: ArtifactOwner,
    artifact_id: int,
    workspace_id: int,
    user_id: int,
) -> None:
    await _ensure_owner(session, owner, workspace_id, True)
    row = await session.scalar(find_requirement_statement(workspace_id, owner, artifact_id))
    if not _is_requirement(row, workspace_id, owner):
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    stored = row
    get_object_storage().delete(stored.oss_ref)
    await session.execute(delete_requirement_statement(workspace_id, owner, artifact_id))
    action = _audit_action(owner, "DELETE_REQUIREMENT_DOC")
    await _audit(
        session,
        workspace_id,
        user_id,
        owner,
        artifact_id,
        stored.name,
        stored.size,
        action,
        None,
    )


async def _store_all(
    session: AsyncSession,
    owner: ArtifactOwner,
    candidates: list[DocumentCandidate],
    workspace_id: int,
    user_id: int,
    source: str,
) -> list[ArtifactView]:
    await _ensure_owner(session, owner, workspace_id, True)
    existing = await _load_documents(session, workspace_id, owner)
    validate_limits(existing, candidates)
    views: list[ArtifactView] = []
    for candidate in candidates:
        views.append(
            await _store_one(session, owner, candidate, workspace_id, user_id, source),
        )
    return views


async def _store_one(
    session: AsyncSession,
    owner: ArtifactOwner,
    candidate: DocumentCandidate,
    workspace_id: int,
    user_id: int,
    source: str,
) -> ArtifactView:
    name = PREFIX + candidate.filename
    key = _owner_path(workspace_id, owner) + candidate.filename
    bucket = requirement_bucket()
    storage = get_object_storage()
    try:
        stored = storage.put(bucket, key, candidate.payload)
    except ObjectStorageError as error:
        raise BizError(ErrorCode.STORAGE_ERROR) from error
    meta = meta_payload(
        source,
        user_id,
        candidate.source_path,
        candidate.content_type,
        candidate.context_kind,
    )
    try:
        result = await session.execute(
            artifact_upsert_statement(
                workspace_id,
                owner,
                name,
                stored.oss_ref,
                stored.size,
                meta,
            ),
        )
    except Exception:
        storage.delete(stored.oss_ref)
        raise
    artifact_id = cast(CursorResult[Any], result).lastrowid
    await _audit(
        session,
        workspace_id,
        user_id,
        owner,
        artifact_id,
        name,
        stored.size,
        _audit_action(owner, "UPLOAD_REQUIREMENT_DOC"),
        source,
    )
    return ArtifactView(
        id=artifact_id,
        workitem_id=owner.source_id,
        dispatch_id=None,
        name=name,
        type=TYPE,
        size=stored.size,
        gmt_create=None,
    )


async def _load_documents(
    session: AsyncSession,
    workspace_id: int,
    owner: ArtifactOwner,
) -> list[Artifact]:
    rows = await session.scalars(list_requirement_statement(workspace_id, owner))
    return list(rows)


async def _ensure_owner(
    session: AsyncSession,
    owner: ArtifactOwner,
    workspace_id: int,
    mutation: bool,
) -> None:
    if owner.source_type == "WORKITEM":
        row = await session.scalar(find_workitem_statement(owner.source_id))
        if row is None or row.tenant_id != workspace_id:
            raise BizError(ErrorCode.WORKITEM_NOT_FOUND)
        return
    if owner.source_type == "SCHEDULED_TASK":
        statement = find_scheduled_task_statement(workspace_id, owner.source_id, mutation)
        task = await session.scalar(statement)
        if task is None:
            raise BizError(ErrorCode.SCHEDULED_TASK_NOT_FOUND)
        if mutation and task.status == "ARCHIVED":
            raise BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE)
        return
    raise BizError(ErrorCode.PARAM_INVALID)


def _is_requirement(
    row: Artifact | None,
    workspace_id: int,
    owner: ArtifactOwner,
) -> TypeGuard[Artifact]:
    if row is None:
        return False
    if row.tenant_id != workspace_id:
        return False
    if execution_source_type(row.source_type) != owner.source_type:
        return False
    if row.workitem_id != owner.source_id:
        return False
    return row.type == TYPE


def _owner_path(workspace_id: int, owner: ArtifactOwner) -> str:
    if owner.source_type == "WORKITEM":
        segment = "workitem"
    else:
        segment = "scheduled-task"
    return "t/" + str(workspace_id) + "/" + segment + "/" + str(owner.source_id) + "/requirements/"


def _audit_action(owner: ArtifactOwner, workitem_action: str) -> str:
    if owner.source_type == "SCHEDULED_TASK":
        return workitem_action.replace("REQUIREMENT_DOC", "SCHEDULED_TASK_REQUIREMENT_DOC")
    return workitem_action


def _strip_prefix(name: str) -> str:
    if name.startswith(PREFIX):
        return name[len(PREFIX) :]
    return name


async def _audit(
    session: AsyncSession,
    workspace_id: int,
    user_id: int,
    owner: ArtifactOwner,
    artifact_id: int,
    name: str,
    size: int | None,
    action: str,
    source: str | None,
) -> None:
    if owner.source_type == "SCHEDULED_TASK":
        actor_type = "HUMAN"
        target_type = "SCHEDULED_TASK"
    else:
        actor_type = "USER"
        target_type = "workitem"
    if source is None:
        trigger_source = "WEB"
    else:
        trigger_source = source
    record = AuditRecord(
        tenant_id=workspace_id,
        actor_id=user_id,
        actor_type=actor_type,
        module="ARTIFACT",
        action=action,
        target_type=target_type,
        target_id=owner.source_id,
        trigger_type="EVENT",
        trigger_source=trigger_source,
        event_type="requirement_document",
    )
    record.add("artifactId", artifact_id)
    record.add("name", name)
    record.add("size", size)
    record.add("sourceType", owner.source_type)
    record.add("sourceId", owner.source_id)
    record.add("source", source)
    await record_required(session, record)


def _require_signature(valid: bool, message: str) -> None:
    if not valid:
        raise BizError(ErrorCode.PARAM_INVALID, message)


def _validate_text(payload: bytes) -> None:
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error


def _validate_word(payload: bytes, content_type: str) -> None:
    if content_type == DOCX_CONTENT_TYPE:
        _validate_zip(payload, DOCX_REQUIRED_ENTRY)
        return
    _require_signature(_has_ole2_signature(payload), "文件内容与 .doc 格式不符")


def _validate_zip(payload: bytes, required_entry: str | None) -> None:
    _require_signature(_has_zip_signature(payload), "文件内容与 ZIP 格式不符")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, EOFError, OSError) as error:
        raise BizError(ErrorCode.PARAM_INVALID, "压缩包无法解析") from error
    try:
        _scan_zip(archive, required_entry)
    except BizError:
        raise
    except (zipfile.BadZipFile, RuntimeError, EOFError, OSError, ValueError) as error:
        raise BizError(ErrorCode.PARAM_INVALID, "压缩包无法解析") from error
    finally:
        archive.close()


def _scan_zip(archive: zipfile.ZipFile, required_entry: str | None) -> None:
    entries = 0
    inflated = 0
    found = required_entry is None
    for info in archive.infolist():
        entries += 1
        if entries > MAX_ZIP_ENTRIES:
            raise BizError(ErrorCode.PARAM_INVALID, "压缩包条目数超过上限 " + str(MAX_ZIP_ENTRIES))
        _validate_zip_entry_name(info.filename)
        if required_entry is not None and info.filename == required_entry:
            found = True
        if _local_header_encrypted(archive, info) or info.flag_bits & 1:
            raise BizError(ErrorCode.PARAM_INVALID, "压缩包无法解析")
        if info.is_dir():
            continue
        with archive.open(info, "r") as stream:
            chunk = stream.read(8192)
            while len(chunk) > 0:
                inflated += len(chunk)
                if inflated > MAX_ZIP_INFLATED_BYTES:
                    limit = MAX_ZIP_INFLATED_BYTES // 1024 // 1024
                    message = "压缩包解压后大小超过上限 " + str(limit) + "MB"
                    raise BizError(ErrorCode.PARAM_INVALID, message)
                chunk = stream.read(8192)
    if not found:
        raise BizError(ErrorCode.PARAM_INVALID, "文件内容与 .docx 格式不符")


def _local_header_encrypted(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    """Java ``ZipInputStream`` 看本地文件头的加密位，不看中央目录。"""
    handle = cast(io.BytesIO, archive.fp)
    handle.seek(info.header_offset + 6)
    flag = handle.read(1)
    return len(flag) == 1 and flag[0] & 1 == 1


def _validate_zip_entry_name(name: str) -> None:
    if _is_blank(name) or name.startswith("/") or name.startswith("\\"):
        raise BizError(ErrorCode.PARAM_INVALID, "压缩包条目名非法，疑似路径穿越")
    if ".." in name or "\\" in name or _DRIVE_LETTER.search(name) is not None:
        raise BizError(ErrorCode.PARAM_INVALID, "压缩包条目名非法，疑似路径穿越")
    depth = 0
    for segment in name.split("/"):
        if segment != "":
            depth += 1
    if depth > MAX_ZIP_PATH_DEPTH:
        message = "压缩包目录层级超过上限 " + str(MAX_ZIP_PATH_DEPTH)
        raise BizError(ErrorCode.PARAM_INVALID, message)


def _has_pdf_signature(payload: bytes) -> bool:
    return (
        len(payload) >= 5
        and payload[0] == ord("%")
        and payload[1] == ord("P")
        and payload[2] == ord("D")
        and payload[3] == ord("F")
        and payload[4] == ord("-")
    )


def _has_zip_signature(payload: bytes) -> bool:
    if len(payload) < 4 or payload[0] != ord("P") or payload[1] != ord("K"):
        return False
    local = payload[2] == 3 and payload[3] == 4
    empty = payload[2] == 5 and payload[3] == 6
    return local or empty


def _has_ole2_signature(payload: bytes) -> bool:
    signature = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1])
    return len(payload) >= 8 and payload[:8] == signature


def _has_image_signature(content_type: str, payload: bytes) -> bool:
    if content_type == "image/png":
        return len(payload) >= 8 and payload[:8] == bytes(
            [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A],
        )
    if content_type == "image/jpeg":
        return (
            len(payload) >= 3 and payload[0] == 0xFF and payload[1] == 0xD8 and payload[2] == 0xFF
        )
    if content_type == "image/webp":
        return len(payload) >= 12 and payload[0:4] == b"RIFF" and payload[8:12] == b"WEBP"
    return False


def _has_text(value: str | None) -> bool:
    if value is None:
        return False
    return not _is_blank(value)


def _is_blank(value: str) -> bool:
    for char in value:
        if not _is_java_whitespace(ord(char)):
            return False
    return True


def _is_java_whitespace(code_point: int) -> bool:
    if code_point in {0x00A0, 0x2007, 0x202F}:
        return False
    if code_point in {0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x001F}:
        return True
    category = unicodedata.category(chr(code_point))
    return category == "Zs" or category == "Zl" or category == "Zp"


def _java_trim(value: str) -> str:
    start = 0
    end = len(value)
    while start < end and ord(value[start]) <= 0x20:
        start += 1
    while end > start and ord(value[end - 1]) <= 0x20:
        end -= 1
    return value[start:end]


def _iso_control(char: str) -> bool:
    code = ord(char)
    return code <= 0x1F or 0x7F <= code <= 0x9F
