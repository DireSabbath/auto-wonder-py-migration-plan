"""创建、列出和下载项目配置备份。"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.backups.archive import (
    BUCKET_MESSAGE,
    MAX_ROWS,
    ROW_LIMIT_MESSAGE,
    Snapshot,
    build_archive,
    check_size,
    encode,
    failure_message,
    require_download,
    select_backup_bucket,
)
from autowonder.backups.models import ProjectBackup
from autowonder.backups.rules import FORMAT_VERSION, RULES
from autowonder.backups.schemas import BackupView
from autowonder.core.errors import BizError, ErrorCode
from autowonder.storage.objects import InMemoryObjectStorage, ObjectStorage, sha256_hex

logger = logging.getLogger(__name__)


async def create_backup(
    session: AsyncSession,
    storage: ObjectStorage,
    workspace_id: int,
    user_id: int,
    backup_bucket: str | None,
    artifact_bucket: str | None,
    base_bucket: str | None,
) -> BackupView:
    """导出当前工作空间配置。存储未持久化时不写历史。"""
    bucket = select_backup_bucket(backup_bucket, artifact_bucket, base_bucket)
    if isinstance(storage, InMemoryObjectStorage):
        raise BizError(ErrorCode.PARAM_INVALID, BUCKET_MESSAGE)
    if bucket is None:
        raise BizError(ErrorCode.PARAM_INVALID, BUCKET_MESSAGE)
    if bucket.strip() == "":
        raise BizError(ErrorCode.PARAM_INVALID, BUCKET_MESSAGE)
    backup_id = str(uuid.uuid4())
    key = "project-backups/" + str(workspace_id) + "/" + backup_id + ".zip"
    ref = bucket + "/" + key
    session.add(
        ProjectBackup(
            id=backup_id,
            tenant_id=workspace_id,
            creator_id=user_id,
            format_version=FORMAT_VERSION,
            status="RUNNING",
        )
    )
    await session.commit()
    try:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        snapshot = await _read_snapshot(session, workspace_id)
        archive = build_archive(snapshot, backup_id, workspace_id, user_id, storage)
        digest = sha256_hex(archive)
        storage.put(bucket, key, archive)
        await session.execute(
            update(ProjectBackup)
            .where(ProjectBackup.id == backup_id, ProjectBackup.tenant_id == workspace_id)
            .values(
                status="SUCCEEDED",
                oss_ref=ref,
                size_bytes=len(archive),
                sha256=digest,
                gmt_finished=text("CURRENT_TIMESTAMP(3)"),
            )
        )
        await session.commit()
    except Exception as failure:
        await session.rollback()
        _delete_object(storage, ref)
        await _mark_failed(session, backup_id, workspace_id, failure_message(failure))
        raise BizError(ErrorCode.SYSTEM_ERROR, failure_message(failure)) from failure
    return await _find(session, workspace_id, backup_id)


async def list_backups(
    session: AsyncSession,
    workspace_id: int,
    page: int,
    size: int,
) -> list[BackupView]:
    """按创建时间倒序分页。"""
    offset = (page - 1) * size
    result = await session.scalars(
        select(ProjectBackup)
        .where(ProjectBackup.tenant_id == workspace_id)
        .order_by(ProjectBackup.gmt_create.desc(), ProjectBackup.id.desc())
        .limit(size)
        .offset(offset)
    )
    return [_to_view(row) for row in result.all()]


async def download_backup(
    session: AsyncSession,
    storage: ObjectStorage,
    workspace_id: int,
    backup_id: str,
) -> str:
    """成功且对象仍在时返回 300 秒下载地址。"""
    backup = await _find(session, workspace_id, backup_id)
    if backup.oss_ref is None:
        present = False
    else:
        present = storage.exists(backup.oss_ref)
    ref = require_download(backup.status, backup.oss_ref, present)
    return storage.presign_get(ref, 300)


async def _read_snapshot(session: AsyncSession, workspace_id: int) -> Snapshot:
    files: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    skills: list[dict[str, Any]] = []
    total = 0
    for rule in RULES:
        result = await session.execute(
            text(rule.sql() + " LIMIT " + str(MAX_ROWS + 1)),
            {"workspace_id": workspace_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
        if len(rows) > MAX_ROWS:
            raise BizError(ErrorCode.PARAM_INVALID, ROW_LIMIT_MESSAGE)
        payload = encode(rows)
        total += len(payload)
        check_size(total)
        files["config/" + rule.table + ".json"] = payload
        counts[rule.table] = len(rows)
        if rule.table == "skill":
            skills = rows
    return Snapshot(files, counts, skills, _captured_at())


async def _find(session: AsyncSession, workspace_id: int, backup_id: str) -> BackupView:
    row = await session.scalar(
        select(ProjectBackup).where(
            ProjectBackup.tenant_id == workspace_id,
            ProjectBackup.id == backup_id,
        )
    )
    if row is None:
        raise BizError(ErrorCode.NOT_FOUND)
    return _to_view(row)


async def _mark_failed(
    session: AsyncSession,
    backup_id: str,
    workspace_id: int,
    message: str,
) -> None:
    try:
        await session.execute(
            update(ProjectBackup)
            .where(ProjectBackup.id == backup_id, ProjectBackup.tenant_id == workspace_id)
            .values(
                status="FAILED",
                error_message=message,
                gmt_finished=text("CURRENT_TIMESTAMP(3)"),
            )
        )
        await session.commit()
    except Exception:
        logger.warning("backup failure history skipped backupId=%s", backup_id, exc_info=True)
        await session.rollback()


def _delete_object(storage: ObjectStorage, ref: str) -> None:
    try:
        storage.delete(ref)
    except Exception:
        logger.warning("backup object cleanup failed ossRef=%s", ref, exc_info=True)


def _to_view(row: ProjectBackup) -> BackupView:
    return BackupView(
        id=row.id,
        status=row.status,
        oss_ref=row.oss_ref,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        error_message=row.error_message,
        created_at=_timestamp_text(row.gmt_create),
        finished_at=_timestamp_text(row.gmt_finished),
    )


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _captured_at() -> str:
    moment = datetime.now(UTC)
    text_value = moment.strftime("%Y-%m-%dT%H:%M:%S.%f")
    whole, fraction = text_value.split(".")
    fraction = fraction.rstrip("0")
    if fraction == "":
        return whole + "Z"
    return whole + "." + fraction + "Z"
