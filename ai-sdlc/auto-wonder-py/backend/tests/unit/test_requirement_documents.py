"""需求文档的白名单、配额、对象路径和上传删除。这些检查不访问数据库。"""

import io
import zipfile
from datetime import datetime

import pytest
from sqlalchemy.dialects import mysql

from autowonder.artifacts.documents import (
    MAX_ZIP_ENTRIES,
    MAX_ZIP_PATH_DEPTH,
    SUPPORTED_EXTENSIONS,
    TYPE,
    ArtifactOwner,
    artifact_upsert_statement,
    delete_requirement_document,
    delete_requirement_statement,
    extension_of,
    file_type_for,
    find_requirement_statement,
    find_scheduled_task_statement,
    find_workitem_statement,
    list_requirement_documents,
    list_requirement_statement,
    meta_payload,
    read_requirement_document,
    replace_clarification_document,
    requirement_bucket,
    sanitize_filename,
    upload_mcp,
    upload_named_files,
    validate_bytes,
    validate_limits,
)
from autowonder.artifacts.models import Artifact
from autowonder.audits.models import AuditLog
from autowonder.core.errors import BizError, ErrorCode
from autowonder.scheduledtasks.models import ScheduledTask
from autowonder.storage.objects import InMemoryObjectStorage, ObjectStorageError
from autowonder.workitems.models import Workitem

PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00])
JPEG = bytes([0xFF, 0xD8, 0xFF, 0x00])
WEBP = b"RIFF" + bytes(4) + b"WEBP" + b"\x00"
OLE2 = bytes([0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1])


def test_supported_extensions_keep_java_order() -> None:
    """扩展名顺序与 Java 白名单一致。"""
    assert SUPPORTED_EXTENSIONS == (
        ".md",
        ".markdown",
        ".txt",
        ".html",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".docx",
        ".doc",
        ".java",
        ".py",
        ".zip",
    )
    assert extension_of("SPEC.MD") == "md"
    assert extension_of("spec.") == ""
    assert extension_of(".hidden") == "hidden"
    assert file_type_for("SPEC.MD") == ("text/markdown", "MARKDOWN")


def test_filename_rejects_paths_controls_and_unknown_types() -> None:
    """路径、空名和控制字符在解析扩展名之前拒绝。未知格式列出白名单。"""
    assert sanitize_filename("  spec.md ") == "spec.md"
    for name in (None, "../spec.md", "a/b.md", "a\\b.md", ".", "..", "a..b.md", "bad\n.md"):
        with pytest.raises(BizError) as caught:
            sanitize_filename(name)
        assert caught.value.code == ErrorCode.PARAM_INVALID.code
    rejected = ("app.exe", "run.sh", "lib.jar", "data.csv", "sheet.xlsx", "no-extension", "spec.")
    for name in rejected:
        with pytest.raises(BizError) as caught:
            file_type_for(sanitize_filename(name))
        assert ".docx" in str(caught.value)
        assert ".zip" in str(caught.value)


def test_text_pdf_and_image_signatures() -> None:
    """文本必须是 UTF-8。PDF 和图片核对魔数。"""
    validate_bytes(b"# Spec", "text/markdown", "MARKDOWN")
    validate_bytes(b"plain notes", "text/plain", "TEXT")
    validate_bytes(b"<html><body>PRD</body></html>", "text/html", "TEXT")
    validate_bytes(b"def helper():\n    return 1\n", "text/x-python", "CODE")
    validate_bytes(b"%PDF-1.4 minimal", "application/pdf", "PDF")
    validate_bytes(PNG, "image/png", "VISUAL")
    validate_bytes(JPEG, "image/jpeg", "VISUAL")
    validate_bytes(WEBP, "image/webp", "VISUAL")
    with pytest.raises(BizError):
        validate_bytes(bytes([0xFF, 0xFE, 0x00]), "text/plain", "TEXT")
    with pytest.raises(BizError) as pdf:
        validate_bytes(b"not-a-pdf", "application/pdf", "PDF")
    assert str(pdf.value) == "文件内容与 PDF 格式不符"
    with pytest.raises(BizError) as image:
        validate_bytes(b"not-an-image", "image/png", "VISUAL")
    assert str(image.value) == "文件内容与图片格式不符"
    with pytest.raises(BizError):
        validate_bytes(PNG, "image/jpeg", "VISUAL")
    with pytest.raises(BizError):
        file_type_for("anim.gif")


def test_archive_and_word_bytes_follow_java_guards() -> None:
    """压缩包限制条目、层级、解压体积和条目名。docx 必须含 word/document.xml。"""
    validate_bytes(_zip({"notes/readme.txt": b"read me"}), "application/zip", "ARCHIVE")
    validate_bytes(_empty_zip(), "application/zip", "ARCHIVE")
    validate_bytes(_docx("验收标准"), _DOCX, "WORD")
    validate_bytes(_docx_with_directory("验收标准"), _DOCX, "WORD")
    validate_bytes(OLE2 + bytes(504), "application/msword", "WORD")
    validate_bytes(_zip(_nested(MAX_ZIP_PATH_DEPTH)), "application/zip", "ARCHIVE")
    plain = _zip({"readme.txt": b"not a word document"})
    with pytest.raises(BizError) as docx:
        validate_bytes(plain, _DOCX, "WORD")
    assert ".docx" in str(docx.value)
    for name in ("../escape.txt", "nested/../../escape.txt", "back\\slash.txt", "/absolute.txt"):
        with pytest.raises(BizError) as caught:
            validate_bytes(_zip({name: b"payload"}), "application/zip", "ARCHIVE")
        assert str(caught.value) == "压缩包条目名非法，疑似路径穿越"
        assert name not in str(caught.value)
    with pytest.raises(BizError) as drive:
        validate_bytes(_zip({"C:/windows/system.txt": b"payload"}), "application/zip", "ARCHIVE")
    assert str(drive.value) == "压缩包条目名非法，疑似路径穿越"
    for name in (" ", "\\escape.txt"):
        with pytest.raises(BizError) as blank:
            validate_bytes(_zip({name: b"payload"}), "application/zip", "ARCHIVE")
        assert str(blank.value) == "压缩包条目名非法，疑似路径穿越"
    validate_bytes(_zip({"a//b.txt": b"payload"}), "application/zip", "ARCHIVE")
    with pytest.raises(BizError) as deep:
        validate_bytes(_zip(_nested(MAX_ZIP_PATH_DEPTH + 1)), "application/zip", "ARCHIVE")
    assert str(deep.value) == "压缩包目录层级超过上限 " + str(MAX_ZIP_PATH_DEPTH)
    with pytest.raises(BizError) as many:
        validate_bytes(_many(MAX_ZIP_ENTRIES + 1), "application/zip", "ARCHIVE")
    assert str(many.value) == "压缩包条目数超过上限 " + str(MAX_ZIP_ENTRIES)
    validate_bytes(_many(MAX_ZIP_ENTRIES), "application/zip", "ARCHIVE")
    with pytest.raises(BizError) as dirs:
        validate_bytes(_files_and_dirs(MAX_ZIP_ENTRIES, 5), "application/zip", "ARCHIVE")
    assert str(dirs.value) == "压缩包条目数超过上限 " + str(MAX_ZIP_ENTRIES)
    with pytest.raises(BizError) as bomb:
        validate_bytes(_zeros(6, 10 * 1024 * 1024), "application/zip", "ARCHIVE")
    assert str(bomb.value) == "压缩包解压后大小超过上限 50MB"
    validate_bytes(_zeros(5, 10 * 1024 * 1024), "application/zip", "ARCHIVE")
    truncated = _zip({"leaked-entry-name.txt": b"payload"})[:32]
    with pytest.raises(BizError) as broken:
        validate_bytes(truncated, "application/zip", "ARCHIVE")
    assert str(broken.value) == "压缩包无法解析"
    assert "leaked-entry-name.txt" not in str(broken.value)
    encrypted = bytearray(_zip({"secret.txt": b"payload"}))
    encrypted[6] |= 0x01
    with pytest.raises(BizError) as locked:
        validate_bytes(bytes(encrypted), "application/zip", "ARCHIVE")
    assert str(locked.value) == "压缩包无法解析"
    for payload in (
        b"PK\x03",
        b"Px\x03\x04",
        b"PK\x03\x99",
        b"PK\x05\x99",
        b"PK\x01\x02",
    ):
        with pytest.raises(BizError) as signature:
            validate_bytes(payload, "application/zip", "ARCHIVE")
        assert str(signature.value) == "文件内容与 ZIP 格式不符"
    with pytest.raises(BizError) as text_zip:
        validate_bytes(b"# Spec", "application/zip", "ARCHIVE")
    assert str(text_zip.value) == "文件内容与 ZIP 格式不符"
    with pytest.raises(BizError) as ole:
        validate_bytes(b"# Spec", "application/msword", "WORD")
    assert str(ole.value) == "文件内容与 .doc 格式不符"
    corrupted = bytearray(OLE2 + bytes(8))
    corrupted[3] ^= 0xFF
    with pytest.raises(BizError) as ole_byte:
        validate_bytes(bytes(corrupted), "application/msword", "WORD")
    assert str(ole_byte.value) == "文件内容与 .doc 格式不符"


def test_limits_count_bytes_and_duplicate_names() -> None:
    """10 份封顶，合计可以正好 20MB，同名是冲突。"""
    existing = [
        _artifact(name="requirements/doc" + str(index) + ".md", size=1) for index in range(10)
    ]
    with pytest.raises(BizError) as count:
        validate_limits(existing, [_candidate("extra.md", b"# Extra")])
    assert count.value.code == ErrorCode.PARAM_INVALID.code
    one = [_artifact(name="requirements/existing.md", size=15 * 1024 * 1024)]
    validate_limits(one, [_candidate("screen.png", PNG + bytes(5 * 1024 * 1024 - len(PNG)))])
    larger = [_artifact(name="requirements/existing.md", size=16 * 1024 * 1024)]
    with pytest.raises(BizError) as total:
        validate_limits(larger, [_candidate("screen2.png", bytes(5 * 1024 * 1024))])
    assert total.value.code == ErrorCode.PARAM_INVALID.code
    named = [_artifact(name="requirements/spec.md", size=10)]
    with pytest.raises(BizError) as duplicate:
        validate_limits(named, [_candidate("spec.md", b"# Spec")])
    assert duplicate.value.code == ErrorCode.CONFLICT.code


def test_meta_omits_blank_source_path_and_keeps_key_order() -> None:
    """来源路径为空时不写入。键顺序与 Fastjson 一致。"""
    meta = meta_payload("MCP", 7, "/tmp/spec.md", "text/markdown", "MARKDOWN")
    assert list(meta) == ["source", "uploaderId", "contentType", "contextKind", "sourcePath"]
    assert meta["uploaderId"] == 7
    blank = meta_payload("MCP", 7, "   ", "text/plain", "TEXT")
    assert "sourcePath" not in blank


def test_bucket_falls_back_when_artifact_bucket_is_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """产物桶没有文本时用默认桶。"""
    monkeypatch.setattr(
        "autowonder.artifacts.documents.get_settings",
        lambda: _Settings("artifact-bucket", "base"),
    )
    assert requirement_bucket() == "artifact-bucket"
    monkeypatch.setattr(
        "autowonder.artifacts.documents.get_settings",
        lambda: _Settings("  ", "base-bucket"),
    )
    assert requirement_bucket() == "base-bucket"


def test_requirement_sql_matches_source_aware_dao() -> None:
    """工单与定时任务使用不同的来源条件。修改任务时锁定行。"""
    workitem = _sql(list_requirement_statement(100, ArtifactOwner("WORKITEM", 3)))
    assert "artifact.source_type = 'WORKITEM'" in workitem
    assert "artifact.type = 'REQUIREMENT_DOC'" in workitem
    assert "ORDER BY artifact.id ASC" in workitem
    task = _sql(list_requirement_statement(20001, ArtifactOwner("SCHEDULED_TASK", 10001)))
    assert "artifact.source_type = 'SCHEDULED_TASK'" in task
    assert "artifact.workitem_id = 10001" in task
    found = _sql(find_requirement_statement(100, ArtifactOwner("WORKITEM", 3), 77))
    assert "artifact.id = 77" in found
    assert "AND artifact.workitem_id" not in found
    scoped = _sql(find_requirement_statement(100, ArtifactOwner("SCHEDULED_TASK", 3), 77))
    assert "artifact.workitem_id = 3" in scoped
    removed = _sql(delete_requirement_statement(100, ArtifactOwner("WORKITEM", 3), 77))
    assert "DELETE FROM artifact" in removed
    assert "artifact.source_type = 'WORKITEM'" in removed
    task_delete = _sql(delete_requirement_statement(100, ArtifactOwner("SCHEDULED_TASK", 3), 77))
    assert "artifact.source_type = 'SCHEDULED_TASK'" in task_delete
    owner = _sql(find_workitem_statement(3))
    assert "workitem.id = 3" in owner
    assert "workitem.is_deleted = 0" in owner
    assert "AND workitem.tenant_id" not in owner
    locked = _sql(find_scheduled_task_statement(100, 5, True))
    assert "scheduled_task.workspace_id = 100" in locked
    assert "FOR UPDATE" in locked
    plain = _sql(find_scheduled_task_statement(100, 5, False))
    assert "FOR UPDATE" not in plain
    upsert = str(
        artifact_upsert_statement(
            100,
            ArtifactOwner("WORKITEM", 3),
            "requirements/spec.md",
            "artifact-bucket/t/100/workitem/3/requirements/spec.md",
            6,
            {"source": "MCP"},
        ).compile(dialect=mysql.dialect()),
    )
    assert "ON DUPLICATE KEY UPDATE" in upsert
    assert "LAST_INSERT_ID(id)" in upsert
    assert "source_type" in upsert
    assert "oss_ref" in upsert


async def test_upload_mcp_stores_markdown_under_the_workitem_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工单文档写到 workitem 路径，审计操作人是 USER。"""
    session, storage = _bind(monkeypatch)
    view = await upload_mcp(
        session,  # type: ignore[arg-type]
        ArtifactOwner("WORKITEM", 3),
        "spec.md",
        b"# Spec",
        100,
        7,
        "/tmp/spec.md",
    )
    assert view.id == 77
    assert view.name == "requirements/spec.md"
    assert view.type == TYPE
    assert storage.get("artifact-bucket/t/100/workitem/3/requirements/spec.md") == b"# Spec"
    audit = _audit(session)
    assert audit.action == "UPLOAD_REQUIREMENT_DOC"
    assert audit.target_type == "workitem"
    detail = audit.detail_json
    assert detail["actorType"] == "USER"
    assert detail["sourceType"] == "WORKITEM"
    assert detail["triggerSource"] == "MCP"
    assert "# Spec" not in detail.values()
    assert session.committed is True


async def test_task_upload_uses_task_path_and_human_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定时任务不查工单，对象键和审计动作都带任务来源。"""
    session, storage = _bind(monkeypatch, task=_task(5, 100, "ACTIVE"))
    await upload_mcp(
        session,  # type: ignore[arg-type]
        ArtifactOwner("SCHEDULED_TASK", 5),
        "spec.md",
        b"# Spec",
        100,
        7,
        None,
    )
    ref = "artifact-bucket/t/100/scheduled-task/5/requirements/spec.md"
    assert storage.get(ref) == b"# Spec"
    assert not any("FROM workitem" in sql for sql in session.sqls)
    assert any("FOR UPDATE" in sql for sql in session.sqls)
    audit = _audit(session)
    assert audit.action == "UPLOAD_SCHEDULED_TASK_REQUIREMENT_DOC"
    assert audit.target_type == "SCHEDULED_TASK"
    assert audit.detail_json["actorType"] == "HUMAN"
    assert audit.detail_json["sourceId"] == 5


async def test_cli_upload_keeps_file_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 按传入顺序保存，审计触发源是 CLI。"""
    session, storage = _bind(monkeypatch)
    views = await upload_named_files(
        session,  # type: ignore[arg-type]
        ArtifactOwner("WORKITEM", 3),
        [("a.md", b"# A"), ("b.png", PNG)],
        100,
        7,
        "CLI",
    )
    assert [view.name for view in views] == ["requirements/a.md", "requirements/b.png"]
    assert storage.get("artifact-bucket/t/100/workitem/3/requirements/a.md") == b"# A"
    audits = [row for row in session.added if isinstance(row, AuditLog)]
    assert [row.detail_json["triggerSource"] for row in audits] == ["CLI", "CLI"]


async def test_duplicate_second_upload_does_not_overwrite_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二次同名上传在写对象前冲突，第一次的正文还在。"""
    session, storage = _bind(monkeypatch, task=_task(5, 100, "ACTIVE"))
    session.doc_lists = [[], [_artifact(id=77, name="requirements/spec.md", size=6)]]
    owner = ArtifactOwner("SCHEDULED_TASK", 5)
    await upload_mcp(session, owner, "spec.md", b"# First", 100, 7, None)  # type: ignore[arg-type]
    with pytest.raises(BizError) as caught:
        await upload_mcp(session, owner, "spec.md", b"# Second", 100, 7, None)  # type: ignore[arg-type]
    assert caught.value.code == ErrorCode.CONFLICT.code
    ref = "artifact-bucket/t/100/scheduled-task/5/requirements/spec.md"
    assert storage.get(ref) == b"# First"


async def test_archived_task_can_be_listed_and_cannot_be_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档任务可以列出。上传走锁定读取并返回 30005。"""
    session, storage = _bind(monkeypatch, task=_task(3, 100, "ARCHIVED"))
    session.docs = [_artifact(id=4, name="requirements/spec.md")]
    owner = ArtifactOwner("SCHEDULED_TASK", 3)
    listed = await list_requirement_documents(session, owner, 100)  # type: ignore[arg-type]
    assert [view.id for view in listed] == [4]
    assert not any("FOR UPDATE" in sql for sql in session.sqls)
    with pytest.raises(BizError) as caught:
        await upload_mcp(session, owner, "spec.md", b"# Spec", 100, 7, None)  # type: ignore[arg-type]
    assert caught.value.code == "30005"
    assert storage._store == {}


async def test_missing_task_and_run_owner_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """找不到任务是 30001。运行记录不能挂需求文档。"""
    session, _storage = _bind(monkeypatch, task=None)
    with pytest.raises(BizError) as missing:
        await list_requirement_documents(
            session,  # type: ignore[arg-type]
            ArtifactOwner("SCHEDULED_TASK", 3),
            100,
        )
    assert missing.value.code == "30001"
    with pytest.raises(BizError) as run:
        await list_requirement_documents(
            session,  # type: ignore[arg-type]
            ArtifactOwner("SCHEDULED_TASK_RUN", 3),
            100,
        )
    assert run.value.code == ErrorCode.PARAM_INVALID.code


async def test_delete_removes_object_and_rejects_another_workitem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除同时去掉对象。另一张工单的文档是 17010。"""
    session, storage = _bind(monkeypatch)
    row = _artifact(
        id=77,
        name="requirements/spec.md",
        oss_ref="artifact-bucket/t/100/workitem/3/requirements/spec.md",
        size=6,
    )
    storage.put("artifact-bucket", "t/100/workitem/3/requirements/spec.md", b"# Spec")
    session.found = row
    await delete_requirement_document(
        session,  # type: ignore[arg-type]
        ArtifactOwner("WORKITEM", 3),
        77,
        100,
        7,
    )
    assert storage.get(row.oss_ref) is None
    assert any(sql.startswith("DELETE") for sql in session.sqls)
    other = _artifact(id=77, workitem_id=4, name="requirements/spec.md")
    session.found = other
    session.committed = False
    with pytest.raises(BizError) as caught:
        await delete_requirement_document(
            session,  # type: ignore[arg-type]
            ArtifactOwner("WORKITEM", 3),
            77,
            100,
            7,
        )
    assert caught.value.code == "17010"
    assert session.committed is False


async def test_replace_clarification_deletes_the_previous_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """澄清文档先删旧对象再写入同名新文件。"""
    session, storage = _bind(monkeypatch)
    old = _artifact(
        id=66,
        name="requirements/clarification.md",
        oss_ref="artifact-bucket/t/100/workitem/3/requirements/clarification.md",
        size=3,
    )
    storage.put("artifact-bucket", "t/100/workitem/3/requirements/clarification.md", b"old")
    session.doc_lists = [[old], []]
    view = await replace_clarification_document(
        session,  # type: ignore[arg-type]
        3,
        "# New clarification",
        100,
        7,
    )
    assert view.name == "requirements/clarification.md"
    ref = "artifact-bucket/t/100/workitem/3/requirements/clarification.md"
    assert storage.get(ref) == b"# New clarification"
    audits = [row.action for row in session.added if isinstance(row, AuditLog)]
    assert audits == ["DELETE_REQUIREMENT_DOC", "UPLOAD_REQUIREMENT_DOC"]


async def test_read_returns_unprefixed_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """下载名去掉 requirements/ 前缀。对象缺失是 17010。"""
    session, storage = _bind(monkeypatch)
    row = _artifact(
        id=77,
        name="requirements/spec.md",
        oss_ref="artifact-bucket/t/100/workitem/3/requirements/spec.md",
    )
    storage.put("artifact-bucket", "t/100/workitem/3/requirements/spec.md", b"# Spec")
    session.found = row
    content = await read_requirement_document(session, 3, 77, 100)  # type: ignore[arg-type]
    assert content.filename == "spec.md"
    assert content.payload == b"# Spec"
    assert content.content_type == "text/markdown"
    session.found = _artifact(
        id=77,
        oss_ref="artifact-bucket/missing",
        name="requirements/spec.md",
    )
    with pytest.raises(BizError) as caught:
        await read_requirement_document(session, 3, 77, 100)  # type: ignore[arg-type]
    assert caught.value.code == "17010"


async def test_storage_failure_does_not_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    """对象存储失败变成 17021，并且不写登记。"""
    session, _storage = _bind(monkeypatch, storage=_FailingStorage())
    with pytest.raises(BizError) as caught:
        await upload_mcp(
            session,  # type: ignore[arg-type]
            ArtifactOwner("WORKITEM", 3),
            "spec.md",
            b"# Spec",
            100,
            7,
            None,
        )
    assert caught.value.code == ErrorCode.STORAGE_ERROR.code
    assert session.added == []
    assert session.committed is False


def _candidate(filename: str, payload: bytes) -> object:
    from autowonder.artifacts.documents import DocumentCandidate

    return DocumentCandidate(filename, payload, None, "text/markdown", "MARKDOWN")


def _artifact(
    id: int = 1,
    name: str = "requirements/spec.md",
    size: int | None = 6,
    workitem_id: int = 3,
    oss_ref: str = "artifact-bucket/t/100/workitem/3/requirements/spec.md",
    source_type: str = "WORKITEM",
) -> Artifact:
    return Artifact(
        id=id,
        tenant_id=100,
        source_type=source_type,
        workitem_id=workitem_id,
        name=name,
        type=TYPE,
        oss_ref=oss_ref,
        size=size,
        gmt_create=datetime(2026, 1, 2, 3, 4, 5),
    )


def _task(task_id: int, workspace_id: int, status: str) -> ScheduledTask:
    moment = datetime(2026, 1, 2, 3, 4, 5)
    return ScheduledTask(
        id=task_id,
        workspace_id=workspace_id,
        name="nightly",
        instruction_md="run",
        squad_id=1,
        initial_agent_id=1,
        schedule_type="ONCE",
        timezone="Asia/Shanghai",
        status=status,
        creator_id=7,
        gmt_create=moment,
        gmt_modified=moment,
    )


def _workitem() -> Workitem:
    moment = datetime(2026, 1, 2, 3, 4, 5)
    return Workitem(
        id=3,
        tenant_id=100,
        work_type="REQ",
        title="需求",
        gmt_create=moment,
        gmt_modified=moment,
    )


class _Settings:
    def __init__(self, artifact_bucket: str, bucket: str) -> None:
        self.oss_artifact_bucket = artifact_bucket
        self.oss_bucket = bucket


class _Cursor:
    lastrowid = 77


class _FailingStorage(InMemoryObjectStorage):
    def put(self, bucket: str, key: str, data: bytes) -> object:
        raise ObjectStorageError("down")


class _Session:
    def __init__(self, task: ScheduledTask | None) -> None:
        self.workitem = _workitem()
        self.task = task
        self.docs: list[Artifact] = []
        self.doc_lists: list[list[Artifact]] | None = None
        self.found: Artifact | None = None
        self.added: list[object] = []
        self.sqls: list[str] = []
        self.committed = False
        self._list_index = 0

    async def scalar(self, statement: object) -> object:
        sql = _text(statement)
        self.sqls.append(sql)
        if "scheduled_task" in sql:
            return self.task
        if "artifact" in sql:
            return self.found
        return self.workitem

    async def scalars(self, statement: object) -> list[Artifact]:
        self.sqls.append(_sql(statement))
        if self.doc_lists is not None:
            rows = self.doc_lists[self._list_index]
            self._list_index += 1
            return rows
        return list(self.docs)

    async def execute(self, statement: object) -> _Cursor:
        self.sqls.append(_text(statement))
        return _Cursor()

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    task: ScheduledTask | None = None,
    storage: InMemoryObjectStorage | None = None,
) -> tuple[_Session, InMemoryObjectStorage]:
    if storage is None:
        storage = InMemoryObjectStorage()
    monkeypatch.setattr("autowonder.artifacts.documents.get_object_storage", lambda: storage)
    monkeypatch.setattr(
        "autowonder.artifacts.documents.get_settings",
        lambda: _Settings("artifact-bucket", "base"),
    )
    return _Session(task), storage


def _audit(session: _Session) -> AuditLog:
    return next(row for row in session.added if isinstance(row, AuditLog))


def _text(statement: object) -> str:
    compiled = statement.compile(dialect=mysql.dialect())  # type: ignore[attr-defined]
    return str(compiled)


def _sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return buffer.getvalue()


def _empty_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w"):
        pass
    return buffer.getvalue()


def _docx(body: str) -> bytes:
    document = (
        '<?xml version="1.0"?><w:document><w:body><w:p><w:t>'
        + body
        + "</w:t></w:p></w:body></w:document>"
    )
    return _zip(
        {
            "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
            "word/document.xml": document.encode(),
        },
    )


def _docx_with_directory(body: str) -> bytes:
    document = "<w:document><w:body><w:p><w:t>" + body + "</w:t></w:p></w:body></w:document>"
    return _zip(
        {
            "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
            "word/": b"",
            "word/document.xml": document.encode(),
        },
    )


def _many(count: int) -> bytes:
    return _zip({"entry-" + str(index) + ".txt": b"x" for index in range(count)})


def _files_and_dirs(files: int, directories: int) -> bytes:
    entries = {"entry-" + str(index) + ".txt": b"x" for index in range(files)}
    for index in range(directories):
        entries["dir-" + str(index) + "/"] = b""
    return _zip(entries)


def _nested(depth: int) -> dict[str, bytes]:
    parts = ["d" + str(index) for index in range(depth - 1)]
    return {"/".join(parts + ["leaf.txt"]): b"leaf"}


def _zeros(entries: int, size: int) -> bytes:
    payload = bytes(size)
    return _zip({"bomb-" + str(index) + ".bin": payload for index in range(entries)})


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
