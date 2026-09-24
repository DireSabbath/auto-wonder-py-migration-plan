"""把配置快照打成 zip。技能包一并纳入，并核对声明的 MD5。"""

import base64
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from autowonder.backups.rules import FORMAT_VERSION
from autowonder.core.errors import BizError, ErrorCode
from autowonder.storage.objects import ObjectStorage, has_text, sha256_hex

MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 100_000
ROW_LIMIT_MESSAGE = "配置数据过多，超过单表 100000 条备份上限"
SIZE_LIMIT_MESSAGE = "备份原始数据超过 64 MiB 上限"
BUCKET_MESSAGE = (
    "请先配置持久化对象存储和 oss.backup-bucket（可回退至 oss.artifact-bucket / oss.bucket）"
)
GENERIC_FAILURE = "备份失败，请检查数据库、对象存储配置及技能包文件后重试"
NOT_READY_MESSAGE = "备份尚未成功，无法下载"
MISSING_FILE_MESSAGE = "备份文件已不存在"

EXCLUDED = (
    "workitems",
    "execution records",
    "conversations",
    "reviews",
    "audit and usage records",
    "access tokens",
    "plaintext secrets",
)
CREDENTIALS = (
    "Secret setting values and callback tokens are omitted; credential_ref values "
    "require the original credential store or manual rebinding. Free-text configuration "
    "is preserved as authored."
)
RESTORE_NOTES = (
    "Configuration export only. IDs and references are preserved. User IDs refer to "
    "existing accounts. Shared squad templates are included. Repository source code and "
    "external URL resources are not copied. Scheduled tasks must be restored disabled."
)


@dataclass
class Snapshot:
    """一次只读快照。"""

    files: dict[str, bytes]
    counts: dict[str, int]
    skills: list[dict[str, Any]]
    captured_at: str


def select_backup_bucket(
    backup_bucket: str | None,
    artifact_bucket: str | None,
    base_bucket: str | None,
) -> str | None:
    """备份桶优先，其次产物桶，最后默认桶。空白视为未配置。"""
    if has_text(backup_bucket):
        return backup_bucket
    if has_text(artifact_bucket):
        return artifact_bucket
    return base_bucket


def page_bounds(page: int, size: int) -> tuple[int, int]:
    """页码至少为 1，每页 1 到 100 条。"""
    if page < 1:
        page = 1
    if size < 1:
        size = 1
    if size > 100:
        size = 100
    return page, size


def require_download(status: str, oss_ref: str | None, exists: bool) -> str:
    """只有成功且对象仍在的备份可以下载。返回对象引用。"""
    if status != "SUCCEEDED":
        raise BizError(ErrorCode.CONFLICT, NOT_READY_MESSAGE)
    if oss_ref is None or not exists:
        raise BizError(ErrorCode.NOT_FOUND, MISSING_FILE_MESSAGE)
    return oss_ref


def failure_message(error: Exception) -> str:
    """业务失败保留原文，其它失败换成不含供应商细节的说明。"""
    if isinstance(error, BizError):
        return str(error)
    return GENERIC_FAILURE


def check_size(total: int) -> None:
    """未压缩内容超过 64 MiB 时停止。"""
    if total > MAX_BYTES:
        raise BizError(ErrorCode.PARAM_INVALID, SIZE_LIMIT_MESSAGE)


def encode(value: object) -> bytes:
    """漂亮打印 JSON。时间写成毫秒时间戳。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        default=_json_default,
    ).encode("utf-8")


def build_archive(
    snapshot: Snapshot,
    backup_id: str,
    workspace_id: int,
    user_id: int,
    storage: ObjectStorage,
) -> bytes:
    """生成含清单和技能包的 zip。条目时间为纪元，便于校验和稳定。"""
    files = snapshot.files
    total = 0
    for content in files.values():
        total += len(content)
    packages: list[dict[str, str]] = []
    for skill in snapshot.skills:
        ref = skill.get("package_oss_ref")
        if ref is None or str(ref).strip() == "":
            continue
        declared = skill.get("package_size")
        if _is_number(declared):
            check_size(total + int(cast(int | float | Decimal, declared)))
        package = storage.get(str(ref))
        if package is None:
            raise BizError(
                ErrorCode.NOT_FOUND,
                "技能包文件缺失，备份未完成（技能 ID：" + str(skill.get("id")) + "）",
            )
        total += len(package)
        check_size(total)
        expected = skill.get("package_md5")
        actual = hashlib.md5(package).hexdigest()
        if expected is not None and str(expected).lower() != actual.lower():
            raise BizError(
                ErrorCode.CONFLICT,
                "技能包校验失败，备份未完成（技能 ID：" + str(skill.get("id")) + "）",
            )
        path = "packages/skill-" + str(skill.get("id")) + ".zip"
        files[path] = package
        packages.append(
            {
                "skillId": str(skill.get("id")),
                "originalOssRef": str(ref),
                "path": path,
            }
        )
    checksums: dict[str, str] = {}
    for path, content in files.items():
        checksums[path] = sha256_hex(content)
    manifest: dict[str, object] = {
        "format": "autowonder-project-config",
        "formatVersion": FORMAT_VERSION,
        "backupId": backup_id,
        "workspaceId": str(workspace_id),
        "createdBy": str(user_id),
        "capturedAt": snapshot.captured_at,
        "rowCounts": snapshot.counts,
        "skillPackages": packages,
        "excluded": list(EXCLUDED),
        "credentials": CREDENTIALS,
        "restoreNotes": RESTORE_NOTES,
        "sha256": checksums,
    }
    files["manifest.json"] = encode(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            info = zipfile.ZipInfo(path)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.flag_bits |= 0x800
            archive.writestr(info, content)
    return output.getvalue()


def _is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float, Decimal))


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            aware = value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        else:
            aware = value
        return int(aware.timestamp() * 1000)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    raise TypeError("Cannot serialize project configuration")
