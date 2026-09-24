"""技能包检查、入库和只读展开，向量对齐 Java 技能包测试。"""

import gzip
import io
import zipfile
from datetime import datetime

import pytest
from sqlalchemy.sql.elements import BindParameter, BooleanClauseList

from autowonder.core.errors import BizError, ErrorCode
from autowonder.skills.models import Skill
from autowonder.skills.package import (
    MAX_PACKAGE_SIZE,
    create_from_package,
    inspect_package,
    list_package_files,
    load_package,
    pack_directory,
    read_package_file,
    update_package,
    upload_mcp_package,
)
from autowonder.skills.router import _download_error
from autowonder.skills.schemas import SkillView
from autowonder.storage.objects import MapObjectStorage

_BUCKET = "artifact-bucket"


class _Cursor:
    def __init__(self, count: int) -> None:
        self.rowcount = count


class SkillSession:
    """记下名称释放、插入和更新的顺序，不连接 MySQL。"""

    def __init__(self) -> None:
        self.skills: list[Skill] = []
        self.events: list[str] = []
        self._pending: list[Skill] = []
        self._next_id = 10000

    def add(self, row: Skill) -> None:
        self._pending.append(row)
        self.events.append("add")

    async def flush(self) -> None:
        row = self._pending.pop()
        if row.id is None:
            row.id = self._next_id
            self._next_id += 1
        self.skills.append(row)
        self.events.append("flush")

    async def execute(self, statement: object) -> _Cursor:
        self.events.append("execute")
        return _Cursor(1)

    async def scalar(self, statement: object) -> object | None:
        entity = statement.column_descriptions[0]["entity"]
        if entity is not Skill:
            return None
        comps = _comparisons(statement.whereclause)
        for skill in self.skills:
            if _matches(skill, comps):
                return skill
        return None

    async def scalars(self, statement: object) -> list[object]:
        return []

    async def commit(self) -> None:
        self.events.append("commit")

    def expire_all(self) -> None:
        return None


class RecordingStorage(MapObjectStorage):
    def __init__(self, session: SkillSession) -> None:
        super().__init__()
        self.session = session

    def put(self, bucket: str, key: str, data: bytes):  # type: ignore[no-untyped-def]
        self.session.events.append("put")
        return super().put(bucket, key, data)


def test_directory_packing_preserves_contents_and_is_stable() -> None:
    """目录打包按路径排序，重试得到同一份 zip，并还原原始字节。"""
    files = {
        "SKILL.md": _b64(_skill_md("demo", "Demo")),
        ".config/empty": "",
        "assets/binary": "AAH/",
    }
    archive = pack_directory(files)
    assert inspect_package("directory.zip", archive)["name"] == "demo"
    reversed_files = {key: files[key] for key in sorted(files, reverse=True)}
    assert pack_directory(reversed_files) == archive
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        unpacked = {name: opened.read(name) for name in opened.namelist()}
    assert set(unpacked) == set(files)
    assert unpacked["assets/binary"] == bytes([0, 1, 255])
    assert unpacked[".config/empty"] == b""


def test_directory_packing_rejects_unsafe_paths_and_limits() -> None:
    """危险路径、空包、坏的 Base64 和超过 500 个文件都拒绝。"""
    for path in ("../escape", "/absolute", "C:/absolute", "a\\b", "a//b", "./a", "folder/"):
        with pytest.raises(BizError):
            pack_directory({path: ""})
    with pytest.raises(BizError):
        pack_directory({})
    with pytest.raises(BizError):
        pack_directory({"SKILL.md": "%%%"})
    many = {f"file{index}": "" for index in range(501)}
    with pytest.raises(BizError):
        pack_directory(many)


def test_inspect_reads_root_skill_frontmatter_from_zip_and_tar_gz() -> None:
    """根上的 SKILL.md 决定名称和说明，嵌套文件不能代替它。"""
    inspected = inspect_package("custom-skill.zip", _skill_zip("custom-skill", "Custom skill"))
    assert inspected["name"] == "custom-skill"
    assert inspected["description"] == "Custom skill"
    assert inspected["fileName"] == "custom-skill.zip"
    assert isinstance(inspected["packageSize"], int)
    assert inspected["packageSize"] > 0
    tar = _tar_gz(
        {
            "SKILL.md": _skill_md("custom-skill", "Custom skill").encode(),
            ".qoder/config.json": b"{}",
            "references/readme.md": b"reference",
        }
    )
    tar_inspected = inspect_package("custom-skill.tar.gz", tar)
    assert tar_inspected["name"] == "custom-skill"
    assert tar_inspected["fileName"] == "custom-skill.tar.gz"
    with pytest.raises(BizError) as raised:
        inspect_package("bad.zip", _zip({"nested/SKILL.md": _skill_md("nested", "desc").encode()}))
    assert raised.value.code == "10001"


def test_inspect_rejects_oversized_entry_and_accepts_assets() -> None:
    """非说明文件解压后超过 100MB 时拒绝；目录和点文件可以留下。"""
    oversized = _zip(
        {
            "SKILL.md": _skill_md("custom-skill", "Custom skill").encode(),
            "assets/large.bin": b"\0" * (MAX_PACKAGE_SIZE + 1),
        }
    )
    with pytest.raises(BizError) as raised:
        inspect_package("custom-skill.zip", oversized)
    assert raised.value.code == "10001"
    accepted = _zip(
        {
            "SKILL.md": _skill_md("custom-skill", "Custom skill").encode(),
            "references/readme.md": b"reference",
            "scripts/run.sh": b"#!/bin/sh\n",
            "assets/logo.txt": b"asset",
            ".qoder/config.json": b"{}",
        }
    )
    assert inspect_package("custom-skill.zip", accepted)["fileName"] == "custom-skill.zip"


def test_upload_rejects_digest_mismatch_and_missing_hook_descriptor() -> None:
    """MD5 不符或 Hook 缺少根描述时不写对象。"""
    storage = MapObjectStorage()
    skill = _skill_zip("custom-skill", "Custom skill")
    with pytest.raises(BizError) as digest:
        upload_mcp_package(
            storage,
            _BUCKET,
            "custom-skill.zip",
            skill,
            "SKILL",
            None,
            None,
            None,
            "wrong-md5",
            1,
        )
    assert digest.value.code == "10001"
    assert storage.puts == []
    with pytest.raises(BizError) as missing:
        upload_mcp_package(
            storage,
            _BUCKET,
            "bad-hook.zip",
            _zip({"scripts/run.sh": b"#!/bin/sh\n"}),
            "HOOK",
            None,
            None,
            None,
            None,
            1,
        )
    assert missing.value.code == "10001"
    assert storage.puts == []


def test_upload_accepts_tool_lifecycle_hooks() -> None:
    """beforeTool 和 afterTool 都是合法 Hook 触发点。"""
    storage = MapObjectStorage()
    for trigger in ("beforeTool", "afterTool"):
        name = "sample-" + trigger
        uploaded = upload_mcp_package(
            storage,
            _BUCKET,
            name + ".zip",
            _hook_zip(name, trigger),
            "HOOK",
            None,
            None,
            None,
            None,
            1,
        )
        assert uploaded.type == "HOOK"
        assert uploaded.name == name


@pytest.mark.asyncio
async def test_create_uploaded_skill_stores_zip_and_releases_name_first() -> None:
    """创建技能前先释放软删名称，对象键使用新主键。"""
    session = SkillSession()
    storage = RecordingStorage(session)
    view = await create_from_package(
        session,  # type: ignore[arg-type]
        storage,
        _BUCKET,
        "custom-skill.zip",
        _skill_zip("custom-skill", "Custom skill for AutoWonder"),
        "SKILL",
        None,
        None,
        None,
        1,
        2,
    )
    assert view.name == "custom-skill"
    assert view.source_type == "OSS_ZIP"
    assert view.package_oss_ref == "artifact-bucket/t/1/skills/10000/skill.zip"
    assert session.skills[0].type == "SKILL"
    assert session.skills[0].source_type == "OSS_ZIP"
    assert session.events.index("execute") < session.events.index("add")
    assert session.events.index("add") < session.events.index("put")


@pytest.mark.asyncio
async def test_create_plugin_and_hook_keep_their_identity() -> None:
    """插件记下兼容运行时，Hook 用根描述里的名称。"""
    plugin_session = SkillSession()
    plugin_storage = RecordingStorage(plugin_session)
    plugin = await create_from_package(
        plugin_session,  # type: ignore[arg-type]
        plugin_storage,
        _BUCKET,
        "team-tools.zip",
        _zip({"plugin.json": b'{"name":"team-tools"}'}),
        "PLUGIN",
        "team-tools",
        "Team tools",
        ["claude"],
        1,
        2,
    )
    assert plugin.type == "PLUGIN"
    assert plugin.install_spec is not None
    assert "claude" in plugin.install_spec
    hook_session = SkillSession()
    hook = await create_from_package(
        hook_session,  # type: ignore[arg-type]
        RecordingStorage(hook_session),
        _BUCKET,
        "sample-before-step.zip",
        _hook_zip("sample-before-step", "beforeStep"),
        "HOOK",
        None,
        None,
        None,
        1,
        2,
    )
    assert hook.type == "HOOK"
    assert hook.name == "sample-before-step"
    assert hook.description == "Runtime lifecycle hook: beforeStep"


@pytest.mark.asyncio
async def test_create_rejects_active_duplicate_before_release() -> None:
    """同名在用技能存在时不释放名称，也不上传。"""
    session = SkillSession()
    session.skills.append(_skill_row(10001, "SKILL", "duplicate-skill"))
    storage = RecordingStorage(session)
    with pytest.raises(BizError) as raised:
        await create_from_package(
            session,  # type: ignore[arg-type]
            storage,
            _BUCKET,
            "duplicate-skill.zip",
            _skill_zip("duplicate-skill", "Updated description"),
            "SKILL",
            None,
            None,
            None,
            1,
            2,
        )
    assert raised.value.code == "22006"
    assert "execute" not in session.events
    assert "add" not in session.events
    assert storage.puts == []


@pytest.mark.asyncio
async def test_update_overwrites_key_and_blocks_other_tenant_or_duplicate() -> None:
    """更新写回原对象键。别的工作空间或重名在写对象前拒绝。"""
    session = SkillSession()
    existing = _skill_row(10000, "SKILL", "custom-skill")
    existing.version = 3
    session.skills.append(existing)
    storage = RecordingStorage(session)
    updated = await update_package(
        session,  # type: ignore[arg-type]
        storage,
        _BUCKET,
        10000,
        "custom-skill-v2.zip",
        _skill_zip("custom-skill-v2", "Updated description"),
        None,
        None,
        None,
        1,
        2,
    )
    assert updated.name == "custom-skill-v2"
    assert storage.puts == [(_BUCKET, "t/1/skills/10000/skill.zip")]
    assert session.events.index("execute") < session.events.index("put")

    foreign = SkillSession()
    other = _skill_row(10000, "SKILL", "custom-skill")
    other.tenant_id = 99
    foreign.skills.append(other)
    foreign_storage = RecordingStorage(foreign)
    with pytest.raises(BizError) as missing:
        await update_package(
            foreign,  # type: ignore[arg-type]
            foreign_storage,
            _BUCKET,
            10000,
            "custom-skill-v2.zip",
            _skill_zip("custom-skill-v2", "Updated description"),
            None,
            None,
            None,
            1,
            2,
        )
    assert missing.value.code == "22001"
    assert foreign_storage.puts == []

    named = SkillSession()
    current = _skill_row(10000, "SKILL", "custom-skill")
    current.version = 3
    named.skills.append(current)
    named.skills.append(_skill_row(10001, "SKILL", "duplicate-skill"))
    named_storage = RecordingStorage(named)
    with pytest.raises(BizError) as duplicate:
        await update_package(
            named,  # type: ignore[arg-type]
            named_storage,
            _BUCKET,
            10000,
            "duplicate-skill.zip",
            _skill_zip("duplicate-skill", "Updated description"),
            None,
            None,
            None,
            1,
            2,
        )
    assert duplicate.value.code == "22006"
    assert named_storage.puts == []
    assert "execute" not in named.events


@pytest.mark.asyncio
async def test_update_same_digest_does_not_upload_again() -> None:
    """包的 MD5 没变时第二次更新不再写对象。"""
    session = SkillSession()
    data = _skill_zip("custom-skill-v2", "Updated description")
    existing = _skill_row(10000, "SKILL", "custom-skill")
    existing.version = 3
    existing.package_md5 = "old-md5"
    session.skills.append(existing)
    storage = RecordingStorage(session)
    first = await update_package(
        session,  # type: ignore[arg-type]
        storage,
        _BUCKET,
        10000,
        "custom-skill-v2.zip",
        data,
        None,
        None,
        None,
        1,
        2,
    )
    second = await update_package(
        session,  # type: ignore[arg-type]
        storage,
        _BUCKET,
        10000,
        "custom-skill-v2.zip",
        data,
        None,
        None,
        None,
        1,
        2,
    )
    assert first.name == "custom-skill-v2"
    assert second.name == "custom-skill-v2"
    assert len(storage.puts) == 1


def test_list_and_read_package_files() -> None:
    """清单补目录并区分文本、图片和二进制；预览返回完整文本。"""
    skill_md = "---\nname: custom-skill\n---\n# 技能说明\n".encode()
    notes = "没有扩展名的纯文本".encode()
    data = _zip(
        {
            "SKILL.md": skill_md,
            "references/guide.md": "参考文档".encode(),
            "assets/logo.png": b"\x89PNG",
            "notes": notes,
            "bin/tool": bytes([0x7F, ord("E"), ord("L"), ord("F"), 0, 1]),
        }
    )
    storage = MapObjectStorage()
    stored = storage.put("skills", "10002/custom-skill.zip", data)
    skill = _view(stored.oss_ref, "custom-skill.zip")
    listing = list_package_files(storage, skill)
    assert listing["format"] == "zip"
    files = listing["files"]
    assert isinstance(files, list)
    by_path = {row["path"]: row for row in files}
    assert by_path["references"]["dir"] is True
    assert by_path["assets"]["kind"] == "DIR"
    assert by_path["SKILL.md"]["kind"] == "TEXT"
    assert by_path["SKILL.md"]["size"] == len(skill_md)
    assert by_path["assets/logo.png"]["kind"] == "IMAGE"
    assert by_path["notes"]["kind"] == "TEXT"
    assert by_path["bin/tool"]["kind"] == "BINARY"
    assert len(files) == 8
    preview = read_package_file(storage, skill, "./references/guide.md")
    assert preview["content"] == "参考文档"
    assert preview["path"] == "references/guide.md"
    assert preview["fileName"] == "guide.md"
    broken = _zip({"SKILL.md": bytes([ord("#"), ord(" "), ord("t"), 0xC3, 0x28, ord("\n")])})
    broken_ref = storage.put("skills", "10002/broken.zip", broken).oss_ref
    content = read_package_file(storage, _view(broken_ref, "broken.zip"), "SKILL.md")
    assert "\ufffd" in str(content["content"])


def test_list_tar_gz_and_gzip_magic_and_missing_package() -> None:
    """tar.gz 按后缀或魔数识别。没有上传包时不读存储。"""
    data = _tar_gz(
        {
            "SKILL.md": b"---\nname: custom-skill\n---\n# s\n",
            "references/guide.md": "参考文档".encode(),
        }
    )
    storage = MapObjectStorage()
    stored = storage.put("skills", "10002/custom-skill.tar.gz", data)
    listing = list_package_files(storage, _view(stored.oss_ref, "custom-skill.tar.gz"))
    assert listing["format"] == "tar.gz"
    files = listing["files"]
    assert isinstance(files, list)
    by_path = {row["path"]: row for row in files}
    assert by_path["references"]["dir"] is True
    assert by_path["references/guide.md"]["size"] == len("参考文档".encode())
    magic = storage.put("skills", "10002/custom-skill", data)
    sniffed = list_package_files(storage, _view(magic.oss_ref, "custom-skill"))
    assert sniffed["format"] == "tar.gz"
    with pytest.raises(BizError) as raised:
        list_package_files(storage, SkillView(id=1, source_type="INSTALL_SPEC"))
    assert str(raised.value) == "该技能无上传包"


def test_package_read_guards_match_java() -> None:
    """显式目录不重复，非法路径不回源，加密包和畸形 tar 拒绝。"""
    explicit = _zip(
        {
            "references/": b"",
            "references/guide.md": "参考文档".encode(),
            "SKILL.md": b"# s",
        }
    )
    storage = MapObjectStorage()
    stored = storage.put("skills", "10002/custom-skill.zip", explicit)
    listing = list_package_files(storage, _view(stored.oss_ref, "custom-skill.zip"))
    files = listing["files"]
    assert isinstance(files, list)
    assert sum(1 for row in files if row["path"] == "references") == 1
    assert len(files) == 3
    preview = read_package_file(
        storage,
        _view(stored.oss_ref, "custom-skill.zip"),
        "references/guide.md",
    )
    assert preview["content"] == "参考文档"
    image = _zip(
        {
            "assets/logo.png": b"\x89PNG",
            "bin/tool": bytes([0x7F, ord("E"), ord("L"), ord("F"), 0]),
        }
    )
    image_ref = storage.put("skills", "10002/image.zip", image).oss_ref
    for path in ("assets/logo.png", "bin/tool"):
        with pytest.raises(BizError) as blocked:
            read_package_file(storage, _view(image_ref, "image.zip"), path)
        assert str(blocked.value) == "该文件不支持在线预览"
    touched = MapObjectStorage()
    planted = touched.put("skills", "10002/custom-skill.zip", b"PK\x03\x04")
    skill = _view(planted.oss_ref, "custom-skill.zip")
    touched.gets.clear()
    rejected_paths = (
        "../SKILL.md",
        "references/../../SKILL.md",
        "/etc/passwd",
        "..",
        "references\\guide.md",
        "  ",
        "./",
    )
    for path in rejected_paths:
        with pytest.raises(BizError) as rejected:
            read_package_file(touched, skill, path)
        assert rejected.value.code == "10001"
    assert touched.gets == []
    many = {f"f{index}.txt": b"x" for index in range(501)}
    crowded = storage.put("skills", "10002/many.zip", _zip(many))
    with pytest.raises(BizError) as too_many:
        list_package_files(storage, _view(crowded.oss_ref, "many.zip"))
    assert "条目数超过上限" in str(too_many.value)
    encrypted = bytearray(_zip({"SKILL.md": b"# s"}))
    encrypted[6] |= 1
    locked = storage.put("skills", "10002/locked.zip", bytes(encrypted))
    with pytest.raises(BizError) as locked_error:
        list_package_files(storage, _view(locked.oss_ref, "locked.zip"))
    assert locked_error.value.code == "10001"
    named_gzip = storage.put("skills", "10002/not-gzip.tar.gz", b"PK\x03\x04")
    with pytest.raises(BizError) as not_gzip:
        list_package_files(storage, _view(named_gzip.oss_ref, "not-gzip.tar.gz"))
    assert not_gzip.value.code == "10001"
    original = _zip({"SKILL.md": b"# s"})
    original_ref = storage.put("skills", "10002/original.zip", original)
    download = load_package(storage, _view(original_ref.oss_ref, "  "))
    assert download.data == original
    assert download.file_name == "original.zip"
    assert download.format == "zip"
    link = _tar_entry("link", "2", "00000000000", b"")
    linked = storage.put("skills", "10002/link.tar.gz", link)
    with pytest.raises(BizError) as symlink:
        list_package_files(storage, _view(linked.oss_ref, "link.tar.gz"))
    assert symlink.value.code == "10001"


def test_download_error_uses_not_found_for_missing_skill() -> None:
    """下载失败体是 JSON。技能不存在是 404，其余是 400。"""
    missing = _download_error(BizError(ErrorCode.SKILL_NOT_FOUND))
    invalid = _download_error(BizError(ErrorCode.PARAM_INVALID, "该技能无上传包"))
    assert missing.status_code == 404
    assert invalid.status_code == 400
    assert missing.headers["x-content-type-options"] == "nosniff"
    assert b"22001" in missing.body


def _skill_row(skill_id: int, skill_type: str, name: str) -> Skill:
    return Skill(
        id=skill_id,
        tenant_id=1,
        type=skill_type,
        name=name,
        description="old",
        source_type="OSS_ZIP",
        install_spec={"source": "OSS_ZIP"},
        is_deleted=0,
        version=0,
        creator_id=2,
        gmt_create=datetime(2026, 9, 24, 8, 0, 0),
        gmt_modified=datetime(2026, 9, 24, 8, 0, 0),
    )


def _view(oss_ref: str, file_name: str) -> SkillView:
    return SkillView(
        id=10002,
        source_type="OSS_ZIP",
        package_oss_ref=oss_ref,
        package_file_name=file_name,
    )


def _skill_md(name: str, description: str) -> str:
    return "---\nname: " + name + "\ndescription: " + description + "\n---\n# " + name + "\n"


def _skill_zip(name: str, description: str) -> bytes:
    return _zip({"SKILL.md": _skill_md(name, description).encode()})


def _hook_zip(name: str, trigger: str) -> bytes:
    hook = (
        "schemaVersion: autowonder.hook.v1\n"
        + "name: "
        + name
        + "\nversion: 1\ntrigger: "
        + trigger
        + "\ninterpreter: bash\ncommand: scripts/run.sh\n"
    )
    return _zip({"hook.yaml": hook.encode(), "scripts/run.sh": b"#!/bin/sh\nexit 0\n"})


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode()).decode()


def _tar_gz(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb") as stream:
        for name, content in entries.items():
            header = bytearray(512)
            _write_tar(header, 0, 100, name)
            _write_tar(header, 100, 8, "0000777")
            _write_tar(header, 108, 8, "0000000")
            _write_tar(header, 116, 8, "0000000")
            _write_tar(header, 124, 12, f"{len(content):011o}")
            _write_tar(header, 136, 12, "00000000000")
            index = 148
            while index < 156:
                header[index] = ord(" ")
                index += 1
            header[156] = ord("0")
            _write_tar(header, 257, 6, "ustar")
            checksum = 0
            for value in header:
                checksum += value
            _write_tar(header, 148, 8, f"{checksum:06o}\0 ")
            stream.write(header)
            stream.write(content)
            padding = (512 - (len(content) % 512)) % 512
            stream.write(b"\0" * padding)
        stream.write(b"\0" * 1024)
    return output.getvalue()


def _tar_entry(name: str, type_flag: str, size_field: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb") as stream:
        header = bytearray(512)
        _write_tar(header, 0, 100, name)
        _write_tar(header, 100, 8, "0000777")
        _write_tar(header, 108, 8, "0000000")
        _write_tar(header, 116, 8, "0000000")
        _write_tar(header, 124, 12, size_field)
        _write_tar(header, 136, 12, "00000000000")
        index = 148
        while index < 156:
            header[index] = ord(" ")
            index += 1
        header[156] = ord(type_flag)
        _write_tar(header, 257, 6, "ustar")
        checksum = 0
        for value in header:
            checksum += value
        _write_tar(header, 148, 8, f"{checksum:06o}\0 ")
        stream.write(header)
        stream.write(content)
        stream.write(b"\0" * 1024)
    return output.getvalue()


def _write_tar(header: bytearray, offset: int, length: int, value: str) -> None:
    raw = value.encode("ascii")
    header[offset : offset + len(raw)] = raw[:length]


def _comparisons(clause: object) -> list[tuple[str, object]]:
    found: list[tuple[str, object]] = []
    _walk(clause, found)
    return found


def _walk(node: object, found: list[tuple[str, object]]) -> None:
    if isinstance(node, BooleanClauseList):
        for child in node.clauses:
            _walk(child, found)
        return
    left = getattr(node, "left", None)
    key = getattr(left, "key", None)
    operator = getattr(getattr(node, "operator", None), "__name__", "")
    right = getattr(node, "right", None)
    if isinstance(key, str) and operator == "eq" and isinstance(right, BindParameter):
        found.append((key, right.value))


def _matches(row: object, comps: list[tuple[str, object]]) -> bool:
    for key, value in comps:
        if getattr(row, key) != value:
            return False
    return True
