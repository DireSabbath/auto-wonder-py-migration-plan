"""按工单或派发列出用户可见产物，并签发下载与预览。"""

from typing import Any, cast

from sqlalchemy import select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from autowonder.artifacts.classification import resolve_artifact_type, user_visible
from autowonder.artifacts.models import Artifact
from autowonder.artifacts.schemas import ArtifactView
from autowonder.core.errors import BizError, ErrorCode
from autowonder.storage.objects import ObjectStorage, get_object_storage

DOWNLOAD_TTL_SECONDS = 600
MAX_PREVIEW_BYTES = 20 * 1024 * 1024
_PREVIEW_EXTENSIONS = frozenset(
    {
        "md",
        "markdown",
        "txt",
        "log",
        "json",
        "jsonl",
        "csv",
        "html",
        "htm",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "mp4",
        "webm",
        "ogg",
        "ogv",
        "mov",
        "m4v",
    }
)
_CONTENT_TYPES = {
    "md": "text/markdown;charset=UTF-8",
    "markdown": "text/markdown;charset=UTF-8",
    "html": "text/html;charset=UTF-8",
    "htm": "text/html;charset=UTF-8",
    "json": "application/json",
    "jsonl": "application/x-ndjson;charset=UTF-8",
    "csv": "text/csv;charset=UTF-8",
    "txt": "text/plain",
    "log": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "ogg": "video/ogg",
    "ogv": "video/ogg",
    "mov": "video/quicktime",
}


def to_artifact_view(row: Artifact) -> ArtifactView:
    """登记行转成查询结果。展示类型按路径补全，不回写存储。"""
    return ArtifactView(
        id=row.id,
        workitem_id=row.workitem_id,
        dispatch_id=row.dispatch_id,
        name=row.name,
        type=resolve_artifact_type(row.type, row.name),
        size=row.size,
        gmt_create=row.gmt_create,
    )


def list_by_workitem_statement(tenant_id: int, workitem_id: int) -> Select[tuple[Artifact]]:
    """工单产物按 id 倒序，只含 WORKITEM 来源。"""
    return (
        select(Artifact)
        .where(
            Artifact.tenant_id == tenant_id,
            Artifact.source_type == "WORKITEM",
            Artifact.workitem_id == workitem_id,
        )
        .order_by(Artifact.id.desc())
    )


def list_by_dispatch_statement(tenant_id: int, dispatch_id: int) -> Select[tuple[Artifact]]:
    """同一派发的产物按 id 倒序。"""
    return (
        select(Artifact)
        .where(Artifact.tenant_id == tenant_id, Artifact.dispatch_id == dispatch_id)
        .order_by(Artifact.id.desc())
    )


def find_by_id_statement(artifact_id: int) -> Select[tuple[Artifact]]:
    """按主键读取，租户在读到行之后核对。"""
    return select(Artifact).where(Artifact.id == artifact_id).limit(1)


def logical_name(name: str | None) -> str:
    """同一执行里 ``artifacts/output`` 与 ``output`` 视为同一个逻辑名。"""
    if name is None:
        return ""
    if name.startswith("artifacts/output/"):
        return name[len("artifacts/output/") :]
    if name.startswith("output/"):
        return name[len("output/") :]
    return name


def visible_workitem_views(rows: list[Artifact]) -> list[ArtifactView]:
    """观测文件不返回。同一派发内同名只保留先出现的一条。"""
    chosen: dict[str, Artifact] = {}
    for row in rows:
        if not user_visible(row.name):
            continue
        key = _dispatch_text(row.dispatch_id) + ":" + logical_name(row.name)
        if key not in chosen:
            chosen[key] = row
    return [to_artifact_view(row) for row in chosen.values()]


def visible_dispatch_views(rows: list[Artifact]) -> list[ArtifactView]:
    """派发详情不去重，只丢掉观测文件。"""
    views: list[ArtifactView] = []
    for row in rows:
        if user_visible(row.name):
            views.append(to_artifact_view(row))
    return views


def file_extension(name: str | None) -> str:
    """去掉查询串和片段后再取最后一个点后的扩展名，并转成小写。"""
    if name is None:
        return ""
    query = name.find("?")
    if query >= 0:
        clean = name[:query]
    else:
        clean = name
    fragment = clean.find("#")
    if fragment >= 0:
        clean = clean[:fragment]
    dot = clean.rfind(".")
    if dot < 0:
        return ""
    return clean[dot + 1 :].lower()


def content_type(name: str | None) -> str:
    """预览响应的 Content-Type。未知扩展名用二进制流。"""
    ext = file_extension(name)
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    return "application/octet-stream"


def is_html(name: str | None) -> bool:
    """html 与 htm 需要 CSP sandbox。"""
    ext = file_extension(name)
    return ext == "html" or ext == "htm"


def preview_status(code: str) -> int:
    """预览失败不走 Result 信封。找不到是 404，未登录是 401，其余是 400。"""
    if code == ErrorCode.UNAUTHORIZED.code:
        return 401
    if code == ErrorCode.ARTIFACT_NOT_FOUND.code:
        return 404
    return 400


async def list_by_workitem(
    session: AsyncSession,
    tenant_id: int,
    workitem_id: int,
) -> list[ArtifactView]:
    """工单产物列表。"""
    rows = await session.scalars(list_by_workitem_statement(tenant_id, workitem_id))
    return visible_workitem_views(list(rows))


async def list_by_dispatch(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> list[ArtifactView]:
    """同一派发的产物按 id 倒序，观测文件不返回。"""
    rows = await session.scalars(list_by_dispatch_statement(tenant_id, dispatch_id))
    return visible_dispatch_views(list(rows))


async def download_url(session: AsyncSession, artifact_id: int, workspace_id: int) -> str:
    """签发 600 秒下载地址。地址原文返回，不再改写协议。"""
    row = await require_artifact(session, artifact_id, workspace_id)
    return get_object_storage().presign_get(row.oss_ref, DOWNLOAD_TTL_SECONDS)


async def preview_bytes(
    session: AsyncSession,
    artifact_id: int,
    workspace_id: int,
) -> tuple[str, bytes]:
    """读取可预览正文。类型不符、大小未知或超过 20MB 时不访问存储。"""
    row = await require_artifact(session, artifact_id, workspace_id)
    return read_preview(row, get_object_storage())


async def require_artifact(
    session: AsyncSession,
    artifact_id: int,
    workspace_id: int,
) -> Artifact:
    """主键存在且属于当前工作空间。"""
    row = await session.scalar(find_by_id_statement(artifact_id))
    if row is None or row.tenant_id != workspace_id:
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    return row


def read_preview(row: Artifact, storage: ObjectStorage) -> tuple[str, bytes]:
    """按扩展名和登记大小决定能否预览，再读取对象。"""
    if not _previewable(row.name):
        raise BizError(ErrorCode.PARAM_INVALID)
    if row.size is None or row.size > MAX_PREVIEW_BYTES:
        raise BizError(ErrorCode.PARAM_INVALID)
    payload = storage.get(row.oss_ref)
    if payload is None:
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    return row.name, payload


def _previewable(name: str | None) -> bool:
    return file_extension(name) in _PREVIEW_EXTENSIONS


def reported_artifact_statement(
    tenant_id: int,
    source_type: str,
    source_id: int,
    dispatch_id: int,
    name: str,
    artifact_type: str,
    oss_ref: str,
    size: int,
) -> Any:
    """与 Java insert 相同：同名冲突时复用主键并覆盖类型、引用和大小。"""
    resolved = resolve_artifact_type(artifact_type, name)
    statement = mysql_insert(Artifact).values(
        tenant_id=tenant_id,
        source_type=source_type,
        workitem_id=source_id,
        dispatch_id=dispatch_id,
        name=name,
        type=resolved,
        oss_ref=oss_ref,
        size=size,
        meta_json=None,
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


async def record_reported_artifact(
    session: AsyncSession,
    tenant_id: int,
    source_type: str,
    source_id: int,
    dispatch_id: int,
    name: str,
    artifact_type: str,
    oss_ref: str,
    size: int,
) -> int:
    """登记执行器上报的产物，并返回行 id。"""
    result = await session.execute(
        reported_artifact_statement(
            tenant_id,
            source_type,
            source_id,
            dispatch_id,
            name,
            artifact_type,
            oss_ref,
            size,
        )
    )
    return cast(CursorResult[Any], result).lastrowid


def _dispatch_text(dispatch_id: int | None) -> str:
    if dispatch_id is None:
        return "null"
    return str(dispatch_id)
