"""技能包的检查、入库和只读展开。zip 与 tar.gz 共用同一套条目限制。"""

import base64
import binascii
import codecs
import gzip
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass

import yaml
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.config import get_settings
from autowonder.core.clock import now_local
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.rows import rowcount
from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.skills.models import Skill
from autowonder.skills.schemas import SkillView
from autowonder.skills.service import (
    _find_live,
    _find_live_name,
    _release_soft_deleted_name,
    get_skill,
)
from autowonder.storage.objects import ObjectStorage, StoredObject

SOURCE_TYPE_OSS_ZIP = "OSS_ZIP"
MAX_ENTRIES = 500
MAX_PACKAGE_SIZE = 100 * 1024 * 1024
SNIFF_LIMIT = 8192
_SKILL = "SKILL"
_PLUGIN = "PLUGIN"
_HOOK = "HOOK"
_KIND_DIR = "DIR"
_KIND_TEXT = "TEXT"
_KIND_IMAGE = "IMAGE"
_KIND_BINARY = "BINARY"
FORMAT_ZIP = "zip"
FORMAT_TAR_GZ = "tar.gz"
_DIRECT_PLUGIN_PROVIDERS = frozenset({"claude", "qoder"})
_HOOK_TRIGGERS = frozenset(
    {
        "beforeRepoPrepare",
        "afterRepoPrepare",
        "beforeAgentStart",
        "afterAgentExit",
        "beforeStep",
        "afterStep",
        "beforeTool",
        "afterTool",
        "beforeCommit",
        "beforePush",
        "onFailure",
        "cleanup",
    }
)
_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp"})
_TEXT_EXTENSIONS = frozenset(
    {
        "md",
        "markdown",
        "mdx",
        "txt",
        "text",
        "log",
        "yaml",
        "yml",
        "json",
        "toml",
        "ini",
        "cfg",
        "conf",
        "properties",
        "env",
        "xml",
        "html",
        "htm",
        "css",
        "scss",
        "less",
        "csv",
        "tsv",
        "js",
        "mjs",
        "cjs",
        "jsx",
        "ts",
        "tsx",
        "vue",
        "svelte",
        "py",
        "rb",
        "sh",
        "bash",
        "zsh",
        "fish",
        "bat",
        "ps1",
        "java",
        "kt",
        "go",
        "rs",
        "c",
        "h",
        "cpp",
        "hpp",
        "cs",
        "php",
        "swift",
        "scala",
        "sql",
        "graphql",
        "proto",
        "gitignore",
        "gitattributes",
        "editorconfig",
    }
)
_HOOK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_FILE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class UploadedPackage:
    """MCP 先上传、后建技能时的对象引用。"""

    package_oss_ref: str
    file_name: str
    size: int
    md5: str
    sha256: str
    type: str
    name: str
    description: str


@dataclass(frozen=True)
class PackageDownload:
    """按原格式下发的技能包字节。"""

    file_name: str
    format: str
    data: bytes


@dataclass(frozen=True)
class _Parsed:
    name: str
    description: str
    file_name: str
    data: bytes


def skill_bucket() -> str:
    """技能桶有正文时用技能桶，否则用默认桶。"""
    settings = get_settings()
    if java_is_blank(settings.oss_skill_bucket):
        return settings.oss_bucket
    return settings.oss_skill_bucket


def inspect_package(file_name: str | None, data: bytes) -> dict[str, object]:
    """从根上的 SKILL.md 读出名称、说明和规范化文件名。"""
    parsed = _parse(file_name, data)
    return {
        "name": parsed.name,
        "description": parsed.description,
        "fileName": parsed.file_name,
        "packageSize": len(parsed.data),
    }


def pack_directory(files: dict[str, str] | None) -> bytes:
    """把相对路径和 Base64 内容打成稳定的 zip。空包、越限和危险路径都拒绝。"""
    if files is None or len(files) == 0 or len(files) > MAX_ENTRIES:
        raise BizError(ErrorCode.PARAM_INVALID)
    output = io.BytesIO()
    total = 0
    try:
        with zipfile.ZipFile(output, "w") as archive:
            for path in sorted(files):
                _validate_entry_name(path)
                if path.endswith("/") or _has_empty_or_dot_part(path):
                    raise BizError(ErrorCode.PARAM_INVALID)
                encoded = files[path]
                if len(encoded) > 4 * ((MAX_PACKAGE_SIZE - total + 2) // 3):
                    raise BizError(ErrorCode.PARAM_INVALID)
                content = base64.b64decode(encoded, validate=True)
                total += len(content)
                if total > MAX_PACKAGE_SIZE:
                    raise BizError(ErrorCode.PARAM_INVALID)
                info = zipfile.ZipInfo(filename=path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
    except BizError:
        raise
    except (binascii.Error, ValueError, OSError, zipfile.BadZipFile) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    return output.getvalue()


def upload_mcp_package(
    storage: ObjectStorage,
    bucket: str,
    file_name: str | None,
    data: bytes,
    skill_type: str | None,
    name: str | None,
    description: str | None,
    providers: list[str] | None,
    expected_md5: str | None,
    tenant_id: int,
) -> UploadedPackage:
    """按内容哈希暂存技能包，并核对调用方给出的 MD5。"""
    normalized = _package_type(skill_type)
    parsed = _parse_by_type(normalized, file_name, data, name, description)
    _verify_digest(data, expected_md5)
    sha256 = _digest(data, "sha256")
    stored = storage.put(
        bucket,
        "t/" + str(tenant_id) + "/skills/packages/" + sha256 + "/" + parsed.file_name,
        data,
    )
    return UploadedPackage(
        stored.oss_ref,
        parsed.file_name,
        stored.size,
        stored.md5,
        sha256,
        normalized,
        parsed.name,
        parsed.description,
    )


async def create_from_package(
    session: AsyncSession,
    storage: ObjectStorage,
    bucket: str,
    file_name: str | None,
    data: bytes,
    skill_type: str | None,
    name: str | None,
    description: str | None,
    providers: list[str] | None,
    tenant_id: int,
    user_id: int,
    idempotency_key: str | None = None,
) -> SkillView:
    """解析技能包，写入技能行，再把原包放到该技能的固定对象键。"""
    return await _create_from_bytes(
        session,
        storage,
        bucket,
        file_name,
        data,
        skill_type,
        name,
        description,
        providers,
        tenant_id,
        user_id,
        idempotency_key,
    )


async def update_package(
    session: AsyncSession,
    storage: ObjectStorage,
    bucket: str,
    skill_id: int,
    file_name: str | None,
    data: bytes,
    name: str | None,
    description: str | None,
    providers: list[str] | None,
    tenant_id: int,
    user_id: int,
    idempotency_key: str | None = None,
) -> SkillView:
    """替换已有技能的包。MD5 相同则直接返回，不重复上传。"""
    _ = idempotency_key
    return await _update_from_bytes(
        session,
        storage,
        bucket,
        skill_id,
        file_name,
        data,
        name,
        description,
        providers,
        tenant_id,
        user_id,
    )


async def create_from_uploaded_package(
    session: AsyncSession,
    storage: ObjectStorage,
    bucket: str,
    package_oss_ref: str | None,
    skill_type: str | None,
    name: str | None,
    description: str | None,
    providers: list[str] | None,
    expected_md5: str | None,
    idempotency_key: str | None,
    tenant_id: int,
    user_id: int,
) -> SkillView:
    """用已经暂存的对象创建技能。MD5 一致且带幂等键时复用同名技能。"""
    loaded = _load_uploaded(storage, package_oss_ref, expected_md5)
    return await _create_from_bytes(
        session,
        storage,
        bucket,
        loaded[0],
        loaded[1],
        skill_type,
        name,
        description,
        providers,
        tenant_id,
        user_id,
        idempotency_key,
    )


async def update_uploaded_package(
    session: AsyncSession,
    storage: ObjectStorage,
    bucket: str,
    skill_id: int,
    package_oss_ref: str | None,
    name: str | None,
    description: str | None,
    providers: list[str] | None,
    expected_md5: str | None,
    idempotency_key: str | None,
    tenant_id: int,
    user_id: int,
) -> SkillView:
    """用已经暂存的对象替换技能包。内容未变时不再写对象。"""
    _ = idempotency_key
    loaded = _load_uploaded(storage, package_oss_ref, expected_md5)
    return await _update_from_bytes(
        session,
        storage,
        bucket,
        skill_id,
        loaded[0],
        loaded[1],
        name,
        description,
        providers,
        tenant_id,
        user_id,
    )


def list_package_files(storage: ObjectStorage, skill: SkillView | None) -> dict[str, object]:
    """列出包内文件，并补上路径里没有单独条目的目录。"""
    ref = _package_ref(storage, skill)
    if ref[1] == ".tar.gz":
        files = _list_tar_gz(ref[2])
    else:
        files = _list_zip(ref[2])
    return {"files": files, "format": _format(ref[1])}


def read_package_file(
    storage: ObjectStorage,
    skill: SkillView | None,
    path: str,
) -> dict[str, object]:
    """读取包内一个文本文件的全部内容。非法路径不会去读对象。"""
    target = _normalize_requested_path(path)
    ref = _package_ref(storage, skill)
    if ref[1] == ".tar.gz":
        content = _read_tar_gz_entry(ref[2], target)
    else:
        content = _read_zip_entry(ref[2], target)
    if content is None:
        raise BizError(ErrorCode.PARAM_INVALID, "包内不存在该文件: " + target)
    if _resolve_kind(target, _sniff_window(content)) != _KIND_TEXT:
        raise BizError(ErrorCode.PARAM_INVALID, "该文件不支持在线预览")
    return {
        "path": target,
        "fileName": _entry_name(target),
        "content": content.decode("utf-8", errors="replace"),
        "binary": False,
    }


def load_package(storage: ObjectStorage, skill: SkillView | None) -> PackageDownload:
    """取回原始包字节，下载端按 zip 或 gzip 下发。"""
    ref = _package_ref(storage, skill)
    return PackageDownload(ref[0], _format(ref[1]), ref[2])


async def _create_from_bytes(
    session: AsyncSession,
    storage: ObjectStorage,
    bucket: str,
    file_name: str | None,
    data: bytes,
    skill_type: str | None,
    name: str | None,
    description: str | None,
    providers: list[str] | None,
    tenant_id: int,
    user_id: int,
    idempotency_key: str | None,
) -> SkillView:
    normalized = _package_type(skill_type)
    parsed = _parse_by_type(normalized, file_name, data, name, description)
    duplicate = await _find_live_name(session, tenant_id, normalized, parsed.name)
    if duplicate is not None:
        if _same_package(idempotency_key, duplicate.package_md5, data):
            return await get_skill(session, duplicate.id)
        raise BizError(ErrorCode.SKILL_DUPLICATE_NAME)
    await _release_soft_deleted_name(session, tenant_id, normalized, parsed.name)
    skill = Skill(
        tenant_id=tenant_id,
        type=normalized,
        name=parsed.name,
        description=parsed.description,
        source_type=SOURCE_TYPE_OSS_ZIP,
        install_spec=_install_spec(normalized, providers),
        creator_id=user_id,
        version=0,
        is_deleted=0,
        gmt_create=now_local(),
        gmt_modified=now_local(),
    )
    session.add(skill)
    await session.flush()
    stored = _put_package(storage, bucket, tenant_id, skill.id, parsed.data)
    await _update_record(
        session,
        skill,
        tenant_id,
        normalized,
        providers,
        parsed,
        stored,
        0,
        user_id,
    )
    await session.commit()
    return await get_skill(session, skill.id)


async def _update_from_bytes(
    session: AsyncSession,
    storage: ObjectStorage,
    bucket: str,
    skill_id: int,
    file_name: str | None,
    data: bytes,
    name: str | None,
    description: str | None,
    providers: list[str] | None,
    tenant_id: int,
    user_id: int,
) -> SkillView:
    existing = await _find_live(session, skill_id)
    if existing is None or existing.tenant_id != tenant_id:
        raise BizError(ErrorCode.SKILL_NOT_FOUND)
    skill_type = _package_type(existing.type)
    parsed = _parse_by_type(skill_type, file_name, data, name, description)
    package_md5 = _digest(parsed.data, "md5")
    if existing.package_md5 is not None and existing.package_md5.lower() == package_md5:
        return await get_skill(session, skill_id)
    duplicate = await _find_live_name(session, tenant_id, skill_type, parsed.name)
    if duplicate is not None and duplicate.id != skill_id:
        raise BizError(ErrorCode.SKILL_DUPLICATE_NAME)
    await _release_soft_deleted_name(session, tenant_id, skill_type, parsed.name)
    stored = _put_package(storage, bucket, tenant_id, skill_id, parsed.data)
    await _update_record(
        session,
        existing,
        tenant_id,
        skill_type,
        providers,
        parsed,
        stored,
        existing.version,
        user_id,
    )
    await session.commit()
    return await get_skill(session, skill_id)


async def _update_record(
    session: AsyncSession,
    skill: Skill,
    tenant_id: int,
    skill_type: str,
    providers: list[str] | None,
    parsed: _Parsed,
    stored: StoredObject,
    version: int,
    user_id: int,
) -> None:
    install_spec = _install_spec(skill_type, providers)
    result = await session.execute(
        update(Skill)
        .where(
            Skill.id == skill.id,
            Skill.tenant_id == tenant_id,
            Skill.version == version,
            Skill.is_deleted == 0,
        )
        .values(
            name=parsed.name,
            type=skill_type,
            description=parsed.description,
            source_type=SOURCE_TYPE_OSS_ZIP,
            package_oss_ref=stored.oss_ref,
            package_file_name=parsed.file_name,
            package_size=stored.size,
            package_md5=stored.md5,
            install_spec=install_spec,
            version=Skill.version + 1,
            modifier_id=user_id,
            gmt_modified=now_local(),
        )
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) == 0:
        raise BizError(ErrorCode.SKILL_VERSION_CONFLICT)
    skill.name = parsed.name
    skill.type = skill_type
    skill.description = parsed.description
    skill.source_type = SOURCE_TYPE_OSS_ZIP
    skill.package_oss_ref = stored.oss_ref
    skill.package_file_name = parsed.file_name
    skill.package_size = stored.size
    skill.package_md5 = stored.md5
    skill.install_spec = install_spec
    skill.version = version + 1
    skill.modifier_id = user_id


def _same_package(idempotency_key: str | None, package_md5: str | None, data: bytes) -> bool:
    if idempotency_key is None or java_is_blank(idempotency_key) or package_md5 is None:
        return False
    return package_md5.lower() == _digest(data, "md5")


def _put_package(
    storage: ObjectStorage,
    bucket: str,
    tenant_id: int,
    skill_id: int,
    data: bytes,
) -> StoredObject:
    key = "t/" + str(tenant_id) + "/skills/" + str(skill_id) + "/skill.zip"
    return storage.put(bucket, key, data)


def _load_uploaded(
    storage: ObjectStorage,
    package_oss_ref: str | None,
    expected_md5: str | None,
) -> tuple[str, bytes]:
    if package_oss_ref is None or java_is_blank(package_oss_ref):
        raise BizError(ErrorCode.PARAM_INVALID)
    data = storage.get(_trim(package_oss_ref))
    if data is None or len(data) == 0 or len(data) > MAX_PACKAGE_SIZE:
        raise BizError(ErrorCode.PARAM_INVALID)
    _verify_digest(data, expected_md5)
    return _file_name_from_ref(package_oss_ref), data


def _package_ref(storage: ObjectStorage, skill: SkillView | None) -> tuple[str, str, bytes]:
    if (
        skill is None
        or skill.source_type != SOURCE_TYPE_OSS_ZIP
        or skill.package_oss_ref is None
        or java_is_blank(skill.package_oss_ref)
    ):
        raise BizError(ErrorCode.PARAM_INVALID, "该技能无上传包")
    oss_ref = _trim(skill.package_oss_ref)
    data = storage.get(oss_ref)
    if data is None or len(data) == 0 or len(data) > MAX_PACKAGE_SIZE:
        raise BizError(ErrorCode.PARAM_INVALID)
    if skill.package_file_name is None or java_is_blank(skill.package_file_name):
        file_name = _file_name_from_ref(oss_ref)
    else:
        file_name = _trim(skill.package_file_name)
    return file_name, _resolve_suffix(file_name, data), data


def _parse_by_type(
    skill_type: str,
    file_name: str | None,
    data: bytes,
    name: str | None,
    description: str | None,
) -> _Parsed:
    if skill_type == _PLUGIN:
        return _parse_plugin(file_name, data, name, description)
    if skill_type == _HOOK:
        return _parse_hook(file_name, data, name, description)
    return _parse(file_name, data)


def _parse(file_name: str | None, data: bytes) -> _Parsed:
    _require_archive(data)
    suffix = _package_suffix(file_name)
    if suffix == ".tar.gz":
        skill_md = _read_root_text_from_tar_gz(data, "SKILL.md")
    else:
        skill_md = _read_root_text_from_zip(data, "SKILL.md")
    metadata = _parse_frontmatter(skill_md)
    return _Parsed(
        metadata[0],
        metadata[1],
        _normalized_file_name(metadata[0], suffix),
        data,
    )


def _parse_plugin(
    file_name: str | None,
    data: bytes,
    name: str | None,
    description: str | None,
) -> _Parsed:
    if len(data) == 0 or len(data) > MAX_PACKAGE_SIZE or name is None or java_is_blank(name):
        raise BizError(ErrorCode.PARAM_INVALID)
    suffix = _package_suffix(file_name)
    if suffix == ".tar.gz":
        files = _validate_tar_gz(data)
    else:
        files = _validate_zip(data)
    if files == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    normalized_name = _trim(name)
    text = ""
    if description is not None:
        text = _trim(description)
    return _Parsed(normalized_name, text, _normalized_file_name(normalized_name, suffix), data)


def _parse_hook(
    file_name: str | None,
    data: bytes,
    requested_name: str | None,
    description: str | None,
) -> _Parsed:
    _require_archive(data)
    suffix = _package_suffix(file_name)
    if suffix != ".zip":
        raise BizError(ErrorCode.PARAM_INVALID)
    hook_yaml = _read_root_text_from_zip(data, "hook.yaml")
    metadata = _parse_hook_metadata(hook_yaml)
    if (
        requested_name is not None
        and not java_is_blank(requested_name)
        and metadata[0] != _trim(requested_name)
    ):
        raise BizError(ErrorCode.PARAM_INVALID)
    if description is None or java_is_blank(description):
        text = "Runtime lifecycle hook: " + metadata[1]
    else:
        text = _trim(description)
    return _Parsed(metadata[0], text, _normalized_file_name(metadata[0], suffix), data)


def _require_archive(data: bytes) -> None:
    if len(data) == 0 or len(data) > MAX_PACKAGE_SIZE:
        raise BizError(ErrorCode.PARAM_INVALID)


def _package_type(skill_type: str | None) -> str:
    if skill_type is None:
        value = _SKILL
    else:
        value = _trim(skill_type).upper()
    if value != _SKILL and value != _PLUGIN and value != _HOOK:
        raise BizError(ErrorCode.PARAM_INVALID)
    return value


def _install_spec(skill_type: str, providers: list[str] | None) -> dict[str, object]:
    if skill_type == _SKILL or skill_type == _HOOK:
        return {"source": SOURCE_TYPE_OSS_ZIP}
    return {"source": SOURCE_TYPE_OSS_ZIP, "providers": _providers(providers)}


def _providers(providers: list[str] | None) -> list[str]:
    if providers is None or len(providers) == 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    normalized: list[str] = []
    for value in providers:
        text = _trim(value).lower()
        if text not in normalized:
            normalized.append(text)
    if len(normalized) == 0 or not _DIRECT_PLUGIN_PROVIDERS.issuperset(normalized):
        raise BizError(ErrorCode.PARAM_INVALID)
    return normalized


def _validate_zip(data: bytes) -> int:
    entries = 0
    files = 0
    inflated = 0
    archive = _open_zip(data)
    for info in archive.infolist():
        _reject_encrypted(data, info)
        entries += 1
        if entries > MAX_ENTRIES:
            raise BizError(ErrorCode.PARAM_INVALID)
        _validate_entry_name(info.filename)
        if info.is_dir():
            continue
        files += 1
        inflated = _consume_zip(archive, info, inflated, False, False)[0]
    return files


def _read_root_text_from_zip(data: bytes, root_file_name: str) -> str:
    count = 0
    inflated = 0
    root_text: str | None = None
    archive = _open_zip(data)
    for info in archive.infolist():
        _reject_encrypted(data, info)
        count += 1
        if count > MAX_ENTRIES:
            raise BizError(ErrorCode.PARAM_INVALID)
        _validate_entry_name(info.filename)
        if info.is_dir():
            continue
        keep = info.filename == root_file_name
        inflated, content = _consume_zip(archive, info, inflated, keep, False)
        if content is not None:
            root_text = content.decode("utf-8", errors="replace")
    if root_text is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    return root_text


def _list_zip(data: bytes) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    listed: set[str] = set()
    entries = 0
    inflated = 0
    archive = _open_zip(data)
    for info in archive.infolist():
        _reject_encrypted(data, info)
        entries += 1
        if entries > MAX_ENTRIES:
            raise _too_many()
        _validate_entry_name(info.filename)
        path = _normalize_entry_path(info.filename)
        if path == "":
            continue
        if info.is_dir():
            _add_directory(files, listed, path)
            continue
        sniff = _kind_by_extension(path) is None
        inflated, head = _consume_zip_head(archive, info, inflated, sniff)
        _add_directory(files, listed, _parent_path(path))
        files.append(_file_row(path, False, head[1], _resolve_kind(path, head[0])))
        listed.add(path)
    return files


def _read_zip_entry(data: bytes, target_path: str) -> bytes | None:
    entries = 0
    inflated = 0
    archive = _open_zip(data)
    for info in archive.infolist():
        _reject_encrypted(data, info)
        entries += 1
        if entries > MAX_ENTRIES:
            raise _too_many()
        _validate_entry_name(info.filename)
        if info.is_dir():
            continue
        if _normalize_entry_path(info.filename) == target_path:
            content = _read_zip_bytes(archive, info)
            if len(content) > MAX_PACKAGE_SIZE:
                raise BizError(ErrorCode.PARAM_INVALID)
            if inflated + len(content) > MAX_PACKAGE_SIZE:
                raise _too_large()
            return content
        inflated = _consume_zip(archive, info, inflated, False, True)[0]
    return None


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error


def _reject_encrypted(data: bytes, info: zipfile.ZipInfo) -> None:
    """本地文件头的加密位与 Java ``ZipInputStream`` 一样直接拒绝。"""
    flags = int.from_bytes(data[info.header_offset + 6 : info.header_offset + 8], "little")
    if flags & 1:
        raise BizError(ErrorCode.PARAM_INVALID)


def _consume_zip(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    inflated: int,
    keep: bool,
    reported: bool,
) -> tuple[int, bytes | None]:
    content = bytearray()
    try:
        with archive.open(info, "r") as stream:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                inflated += len(chunk)
                if inflated > MAX_PACKAGE_SIZE:
                    if reported:
                        raise _too_large()
                    raise BizError(ErrorCode.PARAM_INVALID)
                if keep:
                    content.extend(chunk)
    except BizError:
        raise
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    if keep:
        return inflated, bytes(content)
    return inflated, None


def _consume_zip_head(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    inflated: int,
    sniff: bool,
) -> tuple[int, tuple[bytes | None, int]]:
    head = bytearray()
    size = 0
    try:
        with archive.open(info, "r") as stream:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                if sniff and size < SNIFF_LIMIT:
                    head.extend(chunk[: SNIFF_LIMIT - size])
                size += len(chunk)
                inflated += len(chunk)
                if inflated > MAX_PACKAGE_SIZE:
                    raise _too_large()
    except BizError:
        raise
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    sniffed: bytes | None = None
    if sniff and len(head) > 0:
        sniffed = bytes(head)
    return inflated, (sniffed, size)


def _read_zip_bytes(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        return archive.read(info)
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error


def _validate_tar_gz(data: bytes) -> int:
    return _read_tar_gz(data, None)[0]


def _read_root_text_from_tar_gz(data: bytes, root_file_name: str) -> str:
    root_text = _read_tar_gz(data, root_file_name)[1]
    if root_text is None:
        raise BizError(ErrorCode.PARAM_INVALID)
    return root_text


def _read_tar_gz(data: bytes, root_file_name: str | None) -> tuple[int, str | None]:
    entries = 0
    files = 0
    inflated = 0
    root_text: str | None = None
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            while True:
                header = _read_fully(stream, 512)
                if len(header) != 512:
                    break
                if _is_zero_block(header):
                    break
                entries += 1
                if entries > MAX_ENTRIES:
                    raise BizError(ErrorCode.PARAM_INVALID)
                name = _tar_string(header, 0, 100)
                size = _tar_size(header)
                type_flag = header[156]
                _validate_entry_name(name)
                if type_flag == ord("2"):
                    raise BizError(ErrorCode.PARAM_INVALID)
                if type_flag == ord("5"):
                    continue
                files += 1
                inflated += size
                if inflated > MAX_PACKAGE_SIZE:
                    raise BizError(ErrorCode.PARAM_INVALID)
                if root_file_name is not None and name == root_file_name:
                    content = _read_fully(stream, size)
                    if len(content) != size:
                        raise BizError(ErrorCode.PARAM_INVALID)
                    root_text = content.decode("utf-8", errors="replace")
                else:
                    _skip_fully(stream, size)
                _skip_fully(stream, _tar_padding(size))
    except BizError:
        raise
    except (OSError, EOFError, ValueError) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    return files, root_text


def _list_tar_gz(data: bytes) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    listed: set[str] = set()
    entries = 0
    inflated = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            while True:
                header = _read_fully(stream, 512)
                if len(header) != 512:
                    break
                if _is_zero_block(header):
                    break
                entries += 1
                if entries > MAX_ENTRIES:
                    raise _too_many()
                raw = _tar_string(header, 0, 100)
                size = _tar_size(header)
                type_flag = header[156]
                _validate_entry_name(raw)
                if type_flag == ord("2"):
                    raise BizError(ErrorCode.PARAM_INVALID)
                path = _normalize_entry_path(raw)
                head: bytes | None = None
                if type_flag != ord("5"):
                    if size < 0:
                        raise BizError(ErrorCode.PARAM_INVALID)
                    inflated += size
                    if inflated > MAX_PACKAGE_SIZE:
                        raise _too_large()
                    if path != "" and _kind_by_extension(path) is None:
                        sniff = min(size, SNIFF_LIMIT)
                        head = _read_fully(stream, sniff)
                        _skip_fully(stream, size - len(head))
                    else:
                        _skip_fully(stream, size)
                    _skip_fully(stream, _tar_padding(size))
                if path == "":
                    continue
                if type_flag == ord("5"):
                    _add_directory(files, listed, path)
                    continue
                _add_directory(files, listed, _parent_path(path))
                files.append(_file_row(path, False, size, _resolve_kind(path, head)))
                listed.add(path)
    except BizError:
        raise
    except (OSError, EOFError, ValueError) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    return files


def _read_tar_gz_entry(data: bytes, target_path: str) -> bytes | None:
    entries = 0
    inflated = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            while True:
                header = _read_fully(stream, 512)
                if len(header) != 512:
                    break
                if _is_zero_block(header):
                    break
                entries += 1
                if entries > MAX_ENTRIES:
                    raise _too_many()
                raw = _tar_string(header, 0, 100)
                size = _tar_size(header)
                type_flag = header[156]
                _validate_entry_name(raw)
                if type_flag == ord("2"):
                    raise BizError(ErrorCode.PARAM_INVALID)
                if type_flag == ord("5"):
                    continue
                if size < 0 or size > MAX_PACKAGE_SIZE:
                    raise BizError(ErrorCode.PARAM_INVALID)
                inflated += size
                if inflated > MAX_PACKAGE_SIZE:
                    raise _too_large()
                if _normalize_entry_path(raw) == target_path:
                    content = _read_fully(stream, size)
                    if len(content) != size:
                        raise BizError(ErrorCode.PARAM_INVALID)
                    return content
                _skip_fully(stream, size)
                _skip_fully(stream, _tar_padding(size))
    except BizError:
        raise
    except (OSError, EOFError, ValueError) as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    return None


def _parse_frontmatter(skill_md: str) -> tuple[str, str]:
    if not skill_md.startswith("---\n") and not skill_md.startswith("---\r\n"):
        raise BizError(ErrorCode.PARAM_INVALID)
    if skill_md.startswith("---\r\n"):
        yaml_start = 5
    else:
        yaml_start = 4
    end = skill_md.find("\n---", yaml_start)
    if end < 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    try:
        parsed = yaml.safe_load(skill_md[yaml_start:end])
    except yaml.YAMLError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    if not isinstance(parsed, dict):
        raise BizError(ErrorCode.PARAM_INVALID)
    name = _as_string(parsed.get("name"))
    description = _as_string(parsed.get("description"))
    if name is None or java_is_blank(name) or description is None or java_is_blank(description):
        raise BizError(ErrorCode.PARAM_INVALID)
    return _trim(name), _trim(description)


def _parse_hook_metadata(hook_yaml: str) -> tuple[str, str]:
    try:
        parsed = yaml.safe_load(hook_yaml)
    except yaml.YAMLError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error
    if not isinstance(parsed, dict):
        raise BizError(ErrorCode.PARAM_INVALID)
    schema_version = _as_string(parsed.get("schemaVersion"))
    name = _as_string(parsed.get("name"))
    version = _as_string(parsed.get("version"))
    trigger = _as_string(parsed.get("trigger"))
    command = _as_string(parsed.get("command"))
    if (
        schema_version != "autowonder.hook.v1"
        or name is None
        or _HOOK_NAME.fullmatch(name) is None
        or version is None
        or java_is_blank(version)
        or trigger is None
        or trigger not in _HOOK_TRIGGERS
        or command is None
        or java_is_blank(command)
    ):
        raise BizError(ErrorCode.PARAM_INVALID)
    return name, trigger


def _add_directory(files: list[dict[str, object]], listed: set[str], dir_path: str) -> None:
    if dir_path == "":
        return
    missing: list[str] = []
    current = dir_path
    while current != "" and current not in listed:
        listed.add(current)
        missing.append(current)
        current = _parent_path(current)
    missing.reverse()
    for path in missing:
        files.append(_file_row(path, True, 0, _KIND_DIR))


def _file_row(path: str, directory: bool, size: int, kind: str) -> dict[str, object]:
    return {
        "path": path,
        "name": _entry_name(path),
        "dir": directory,
        "size": size,
        "kind": kind,
    }


def _parent_path(path: str) -> str:
    index = path.rfind("/")
    if index < 0:
        return ""
    return path[:index]


def _entry_name(path: str) -> str:
    index = path.rfind("/")
    if index < 0:
        return path
    return path[index + 1 :]


def _normalize_entry_path(name: str) -> str:
    path = _trim(name)
    while path.startswith("./"):
        path = path[2:]
    while path.endswith("/"):
        path = path[: len(path) - 1]
    return path


def _normalize_requested_path(path: str) -> str:
    _validate_entry_name(path)
    normalized = _normalize_entry_path(path)
    if normalized == "":
        raise BizError(ErrorCode.PARAM_INVALID)
    return normalized


def _resolve_kind(path: str, head: bytes | None) -> str:
    by_extension = _kind_by_extension(path)
    if by_extension is not None:
        return by_extension
    if _looks_textual(head):
        return _KIND_TEXT
    return _KIND_BINARY


def _kind_by_extension(path: str) -> str | None:
    extension = _extension(path)
    if extension == "":
        return None
    if extension in _IMAGE_EXTENSIONS:
        return _KIND_IMAGE
    if extension in _TEXT_EXTENSIONS:
        return _KIND_TEXT
    return None


def _extension(path: str) -> str:
    name = _entry_name(path).lower()
    index = name.rfind(".")
    if index < 0:
        return ""
    if index == 0:
        return name[1:]
    return name[index + 1 :]


def _sniff_window(content: bytes) -> bytes:
    if len(content) <= SNIFF_LIMIT:
        return content
    return content[:SNIFF_LIMIT]


def _looks_textual(head: bytes | None) -> bool:
    if head is None or len(head) == 0:
        return True
    for value in head:
        if value == 0:
            return False
        if value < 0x09 or (value > 0x0D and value < 0x20 and value != 0x1B):
            return False
    return _decodable_utf8(head)


def _decodable_utf8(head: bytes) -> bool:
    """末尾被截断的多字节序列不算非法，中间的坏字节算。"""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        decoder.decode(head, False)
    except UnicodeDecodeError:
        return False
    return True


def _format(suffix: str) -> str:
    if suffix == ".tar.gz":
        return FORMAT_TAR_GZ
    return FORMAT_ZIP


def _resolve_suffix(file_name: str | None, data: bytes) -> str:
    if file_name is not None and not java_is_blank(file_name):
        normalized = _trim(file_name).lower()
        if normalized.endswith(".tar.gz"):
            return ".tar.gz"
        if normalized.endswith(".zip"):
            return ".zip"
    if _is_gzip(data):
        return ".tar.gz"
    return ".zip"


def _is_gzip(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B


def _package_suffix(file_name: str | None) -> str:
    if file_name is None or java_is_blank(file_name):
        return ".zip"
    normalized = _trim(file_name).lower()
    if normalized.endswith(".tar.gz"):
        return ".tar.gz"
    if normalized.endswith(".zip"):
        return ".zip"
    raise BizError(ErrorCode.PARAM_INVALID)


def _normalized_file_name(name: str, suffix: str) -> str:
    return _FILE_NAME.sub("-", name) + suffix


def _file_name_from_ref(package_oss_ref: str) -> str:
    index = package_oss_ref.rfind("/")
    if index < 0:
        return package_oss_ref
    return package_oss_ref[index + 1 :]


def _validate_entry_name(name: str | None) -> None:
    if (
        name is None
        or java_is_blank(name)
        or name.startswith("/")
        or name.startswith("\\")
        or ".." in name
        or "\\" in name
        or ":" in name
        or "\0" in name
    ):
        raise BizError(ErrorCode.PARAM_INVALID)


def _has_empty_or_dot_part(path: str) -> bool:
    for part in path.split("/"):
        if part == "" or part == ".":
            return True
    return False


def _verify_digest(data: bytes, expected_md5: str | None) -> None:
    if expected_md5 is None or java_is_blank(expected_md5):
        return
    if _trim(expected_md5).lower() != _digest(data, "md5"):
        raise BizError(ErrorCode.PARAM_INVALID)


def _digest(data: bytes, algorithm: str) -> str:
    if algorithm == "sha256":
        return hashlib.sha256(data).hexdigest()
    return hashlib.md5(data).hexdigest()


def _as_string(value: object) -> str | None:
    """与 Java ``String.valueOf`` 一致，布尔值写成小写。"""
    if value is None:
        return None
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _trim(value: str) -> str:
    """``String.trim``：只去掉码点不大于 U+0020 的字符。"""
    start = 0
    end = len(value)
    while start < end and ord(value[start]) <= 0x20:
        start += 1
    while end > start and ord(value[end - 1]) <= 0x20:
        end -= 1
    return value[start:end]


def _read_fully(stream: gzip.GzipFile, size: int) -> bytes:
    if size < 0:
        raise BizError(ErrorCode.PARAM_INVALID)
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _skip_fully(stream: gzip.GzipFile, size: int) -> None:
    remaining = size
    while remaining > 0:
        skipped = stream.read(min(remaining, 8192))
        if not skipped:
            raise BizError(ErrorCode.PARAM_INVALID)
        remaining -= len(skipped)


def _is_zero_block(header: bytes) -> bool:
    for value in header:
        if value != 0:
            return False
    return True


def _tar_string(header: bytes, offset: int, length: int) -> str:
    end = offset
    limit = offset + length
    while end < limit and header[end] != 0:
        end += 1
    return _trim(header[offset:end].decode("utf-8", errors="replace"))


def _tar_size(header: bytes) -> int:
    value = _trim(_tar_string(header, 124, 12))
    if value == "":
        return 0
    try:
        return int(value, 8)
    except ValueError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error


def _tar_padding(size: int) -> int:
    remainder = size % 512
    if remainder == 0:
        return 0
    return 512 - remainder


def _too_many() -> BizError:
    return BizError(ErrorCode.PARAM_INVALID, "技能包条目数超过上限 " + str(MAX_ENTRIES))


def _too_large() -> BizError:
    return BizError(ErrorCode.PARAM_INVALID, "技能包解压后大小超过上限 100MB")
