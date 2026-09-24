"""CLI 需求文档令牌、上传和下载。这些检查不访问数据库。"""

import asyncio
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.artifacts.cli_tokens import (
    DOWNLOAD_ENV,
    DOWNLOAD_PREFIX,
    DOWNLOAD_PURPOSE,
    UPLOAD_ENV,
    UPLOAD_PREFIX,
    UPLOAD_PURPOSE,
    CredentialType,
    authenticate_download,
    authenticate_upload,
    download_command_template_for,
    download_token_env_hint,
    find_any_scheduled_task_statement,
    mint_download_token,
    mint_upload_token,
    posix_quote,
    powershell_quote,
    presented_token,
    scheduled_task_command_template_for,
    upload_command_template_for,
    upload_token_env_hint,
)
from autowonder.artifacts.documents import SUPPORTED_EXTENSIONS, TYPE
from autowonder.artifacts.models import Artifact
from autowonder.audits.models import AuditLog
from autowonder.core.errors import BizError, ErrorCode
from autowonder.db.session import get_session
from autowonder.main import create_app
from autowonder.scheduledtasks.models import ScheduledTask
from autowonder.security.jwt import sign_user_purpose
from autowonder.storage.objects import InMemoryObjectStorage
from autowonder.workitems.models import Workitem
from autowonder.workspaces.models import OrgMember

USER_ID = 7
TENANT_ID = 100
WORKITEM_ID = 50063
TASK_ID = 321
SECRET = "test-secret-key-that-is-long-enough-32bytes!"
PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00])
BASE = "https://daily.auto-wonder.example.com"
VERSION = "0.2.130"


def test_shell_quotes_follow_java_replacements() -> None:
    """POSIX 把单引号拆开，PowerShell 把单引号加倍。"""
    assert posix_quote("a'b") == "'a'\\''b'"
    assert powershell_quote("a'b") == "'a''b'"


def test_any_scheduled_task_lookup_ignores_workspace() -> None:
    """CLI 先按主键找到任务，工作空间留到成员校验。"""
    sql = " ".join(_bound(find_any_scheduled_task_statement(TASK_ID)).split())
    assert "WHERE scheduled_task.id = 321 AND scheduled_task.is_deleted = 0 LIMIT 1" in sql


async def test_upload_token_uses_deployment_and_hides_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长效凭证签发 30 分钟上传令牌，命令里重复列出文件。"""
    session = _ready()
    _patch(monkeypatch, "http://autowonder.internal.example.com:8080", "0.9.9-rc.1")
    view = await mint_upload_token(session, CredentialType.LONG_LIVED, USER_ID, WORKITEM_ID)
    assert view.token.startswith(UPLOAD_PREFIX)
    assert SECRET not in view.token
    assert view.token_type == "Bearer"
    assert view.expires_in_seconds == 1800
    expires_at = datetime.fromisoformat(view.expires_at.replace("Z", "+00:00"))
    assert abs(int(expires_at.timestamp()) - (int(datetime.now(UTC).timestamp()) + 1800)) <= 5
    assert view.server_url == "http://autowonder.internal.example.com:8080"
    assert view.runtime_version == "0.9.9-rc.1"
    assert view.token_env_name == UPLOAD_ENV
    assert view.supported_extensions == list(SUPPORTED_EXTENSIONS)
    assert {".docx", ".doc", ".java", ".py", ".zip"} <= set(view.supported_extensions)
    assert view.max_files == 10
    assert view.max_file_size_bytes == 5 * 1024 * 1024
    assert view.max_total_size_bytes == 20 * 1024 * 1024
    for command in (view.command, view.powershell_command):
        assert "autowonder@0.9.9-rc.1" in command
        assert "http://autowonder.internal.example.com:8080" in command
        assert "--workitem-id " + str(WORKITEM_ID) in command
        assert "--file <filepath-1>" in command
        assert "--file <filepath-2>" in command
        assert "--file <images-1>" in command
        assert "--json" in command
        assert "autowonder@latest" not in command
    assert view.command.startswith("export " + UPLOAD_ENV + "='awupload_")
    assert view.powershell_command.startswith("$env:" + UPLOAD_ENV + "='awupload_")
    assert authenticate_upload(view.token) == USER_ID


async def test_command_templates_follow_branding_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """保存的域名立刻进入命令；清空后回到部署根地址，并去掉结尾斜杠。"""
    session = _ready()
    _patch(monkeypatch, "https://autowonder.example.com/", VERSION)
    session.branding = _Brand("https://wonder.example.com")
    view = await mint_upload_token(session, CredentialType.LONG_LIVED, USER_ID, WORKITEM_ID)
    assert view.server_url == "https://wonder.example.com"
    assert "--server-url 'https://wonder.example.com'" in view.command
    assert "--server-url 'https://wonder.example.com'" in view.powershell_command
    upload = await upload_command_template_for(session)
    scheduled = await scheduled_task_command_template_for(session)
    assert "--server-url https://wonder.example.com " in upload
    assert "--server-url https://wonder.example.com " in scheduled
    assert scheduled.startswith("npx -y autowonder@0.2.130 scheduled-task upload")
    assert "--scheduled-task-id <scheduled-task-id>" in scheduled
    assert scheduled.endswith("--json")
    assert UPLOAD_PREFIX not in upload
    assert upload_token_env_hint() == (
        "export " + UPLOAD_ENV + "='<token returned by autowonder.workitem_cli_upload_token>'"
    )
    session.branding = _Brand(None)
    cleared = await mint_upload_token(session, CredentialType.LONG_LIVED, USER_ID, WORKITEM_ID)
    assert cleared.server_url == "https://autowonder.example.com"
    template = await upload_command_template_for(session)
    assert "--server-url https://autowonder.example.com " in template


async def test_dispatch_and_conversation_credentials_mint_upload_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调度和会话凭证与长效凭证一样可以签发上传令牌。"""
    _patch(monkeypatch)
    for credential in (CredentialType.DISPATCH, CredentialType.CONVERSATION):
        view = await mint_upload_token(_ready(), credential, USER_ID, WORKITEM_ID)
        assert view.token.startswith(UPLOAD_PREFIX)
        assert authenticate_upload(view.token) == USER_ID


async def test_upload_mint_requires_active_write_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失、停用、已删除或只读成员不能签发上传令牌。"""
    _patch(monkeypatch)
    for member in (
        None,
        _member("READ_ONLY"),
        _member("READ_WRITE", status=1),
        _member("READ_WRITE", deleted=1),
        _member("OWNER"),
    ):
        session = _ready()
        session.members[(TENANT_ID, USER_ID)] = member
        with pytest.raises(BizError) as caught:
            await mint_upload_token(session, CredentialType.LONG_LIVED, USER_ID, WORKITEM_ID)
        assert caught.value.code == ErrorCode.NO_PERMISSION.code


async def test_admin_can_mint_and_missing_or_foreign_workitem_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理员可以签发。工单不存在是 13003，别的空间没有成员是 10403。"""
    _patch(monkeypatch)
    session = _ready()
    session.members[(TENANT_ID, USER_ID)] = _member("ADMIN")
    view = await mint_upload_token(session, CredentialType.LONG_LIVED, USER_ID, WORKITEM_ID)
    assert view.token.startswith(UPLOAD_PREFIX)
    missing = _ready()
    missing.workitems.clear()
    with pytest.raises(BizError) as absent:
        await mint_upload_token(missing, CredentialType.DISPATCH, USER_ID, WORKITEM_ID)
    assert absent.value.code == ErrorCode.WORKITEM_NOT_FOUND.code
    foreign = _ready()
    foreign.workitems[WORKITEM_ID] = _workitem(WORKITEM_ID, 999)
    with pytest.raises(BizError) as denied:
        await mint_upload_token(foreign, CredentialType.CONVERSATION, USER_ID, WORKITEM_ID)
    assert denied.value.code == ErrorCode.NO_PERMISSION.code


async def test_authenticate_upload_rejects_invalid_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """前缀、用途、过期和篡改都是 10401，错误里不带令牌原文。"""
    _patch(monkeypatch)
    wrong = UPLOAD_PREFIX + sign_user_purpose(USER_ID, "dispatch-mcp", 1800)
    expired = UPLOAD_PREFIX + sign_user_purpose(USER_ID, UPLOAD_PURPOSE, -1)
    valid = UPLOAD_PREFIX + sign_user_purpose(USER_ID, UPLOAD_PURPOSE, 1800)
    tampered = valid[:-4] + "AAAA"
    for token in (None, "", "awdispatch_x", wrong, expired, tampered):
        with pytest.raises(BizError) as caught:
            authenticate_upload(token)
        assert caught.value.code == ErrorCode.UNAUTHORIZED.code
        if token:
            assert token not in str(caught.value)


async def test_download_token_allows_read_only_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只读成员可以签发下载令牌，命令指向 workitem download。"""
    session = _ready()
    session.members[(TENANT_ID, USER_ID)] = _member("READ_ONLY")
    _patch(monkeypatch, BASE, VERSION)
    view = await mint_download_token(session, CredentialType.LONG_LIVED, USER_ID, WORKITEM_ID)
    assert view.token.startswith(DOWNLOAD_PREFIX)
    assert view.token_env_name == DOWNLOAD_ENV
    assert view.supported_extensions == list(SUPPORTED_EXTENSIONS)
    assert not hasattr(view, "max_files")
    assert view.command.startswith("export " + DOWNLOAD_ENV + "='awdownload_")
    assert "workitem download" in view.command
    assert "--file <name-or-id>" in view.command
    assert "--output-dir <dir>" in view.powershell_command
    assert authenticate_download(view.token) == USER_ID
    template = await download_command_template_for(session)
    assert template == (
        "npx -y autowonder@"
        + VERSION
        + " workitem download --server-url "
        + BASE
        + " --workitem-id <workitem-id>"
        + " --file <name-or-id> --output-dir <dir> --json"
    )
    assert download_token_env_hint().startswith("export " + DOWNLOAD_ENV + "='")
    assert DOWNLOAD_PREFIX not in download_token_env_hint()
    with pytest.raises(BizError) as caught:
        authenticate_download(view.token.replace(DOWNLOAD_PREFIX, UPLOAD_PREFIX, 1))
    assert caught.value.code == ErrorCode.UNAUTHORIZED.code


async def test_download_mint_rejects_inactive_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    """停用成员不能签发下载令牌。"""
    _patch(monkeypatch)
    session = _ready()
    session.members[(TENANT_ID, USER_ID)] = _member("READ_ONLY", status=1)
    with pytest.raises(BizError) as caught:
        await mint_download_token(session, CredentialType.DISPATCH, USER_ID, WORKITEM_ID)
    assert caught.value.code == ErrorCode.NO_PERMISSION.code


def test_cli_routes_are_open_and_token_failures_stay_in_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """这些路径不走会话令牌。控制器的 401 仍写出 data 字段。"""
    session = _ready()
    client = _client(session, monkeypatch)
    paths = client.app.openapi()["paths"]
    upload = "/api/cli/workitems/{workitemId}/requirement-documents"
    assert "post" in paths[upload]
    assert "get" in paths[upload + "/index"]
    content = upload + "/{artifactId}/content"
    assert "get" in paths[content]
    assert "post" in paths["/api/cli/scheduled-tasks/{taskId}/documents"]
    denied = client.get(_index(WORKITEM_ID))
    assert denied.status_code == 401
    assert denied.json()["code"] == "10401"
    assert "data" in denied.json()


def test_valid_token_uploads_files_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """上传令牌按表单顺序保存文件，响应里不回显令牌。"""
    session = _ready()
    storage = _client_storage(monkeypatch)
    client = _client(session, monkeypatch, storage)
    token = _mint(session)
    response = client.post(
        _upload(WORKITEM_ID),
        files=[
            _file("requirements.md", b"# Requirements", "text/markdown"),
            _file("design.md", b"# Design", "text/markdown"),
            _file("architecture.png", PNG, "image/png"),
        ],
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert [item["name"] for item in body["data"]] == [
        "requirements/requirements.md",
        "requirements/design.md",
        "requirements/architecture.png",
    ]
    assert token not in response.text
    stored = storage.get(_workitem_ref(TENANT_ID, WORKITEM_ID, "requirements.md"))
    assert stored == b"# Requirements"
    audits = [row for row in session.added if isinstance(row, AuditLog)]
    assert [row.action for row in audits] == ["UPLOAD_REQUIREMENT_DOC"] * 3
    assert [row.detail_json["triggerSource"] for row in audits] == ["CLI"] * 3


def test_bearer_scheme_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bearer 的大小写不影响上传和下载。NBSP 不会被当成空白去掉。"""
    session = _ready()
    client = _client(session, monkeypatch)
    upload_token = _mint(session)
    download_token = _mint_download(session)
    for scheme in ("bearer ", "BEARER ", "BeArEr "):
        uploaded = client.post(
            _upload(WORKITEM_ID),
            files=[_file("scheme.md", b"# Scheme", "text/markdown")],
            headers={"Authorization": scheme + upload_token},
        )
        assert uploaded.status_code == 200
        listed = client.get(
            _index(WORKITEM_ID),
            headers={"Authorization": scheme + download_token},
        )
        assert listed.status_code == 200
    with pytest.raises(BizError) as caught:
        authenticate_upload(presented_token("Bearer \u00a0" + upload_token))
    assert caught.value.code == ErrorCode.UNAUTHORIZED.code


def test_malformed_upload_credentials_return_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失、过期、串用途和篡改的上传令牌都是 401。"""
    session = _ready()
    client = _client(session, monkeypatch)
    expired = UPLOAD_PREFIX + sign_user_purpose(USER_ID, UPLOAD_PURPOSE, -1)
    wrong = UPLOAD_PREFIX + sign_user_purpose(USER_ID, "dispatch-mcp", 1800)
    valid = _mint(session)
    headers = [
        None,
        "Bearer",
        "Bearer ",
        "Bearer awdispatch_x",
        "Bearer " + expired,
        "Bearer " + wrong,
        "Bearer " + valid[:-4] + "AAAA",
    ]
    for header in headers:
        kwargs: dict[str, object] = {
            "files": [_file("a.md", b"# A", "text/markdown")],
        }
        if header is not None:
            kwargs["headers"] = {"Authorization": header}
        response = client.post(_upload(WORKITEM_ID), **kwargs)  # type: ignore[arg-type]
        assert response.status_code == 401
        assert response.json()["code"] == "10401"
        assert valid not in response.text


def test_upload_membership_and_workitem_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """工单不存在是 404。只读、停用或已删除成员是 403。"""
    session = _ready()
    client = _client(session, monkeypatch)
    token = _mint(session)
    session.workitems.clear()
    missing = client.post(
        _upload(WORKITEM_ID),
        files=[_file("a.md", b"# A", "text/markdown")],
        headers={"Authorization": "Bearer " + token},
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == ErrorCode.WORKITEM_NOT_FOUND.code
    session.workitems[WORKITEM_ID] = _workitem(WORKITEM_ID, TENANT_ID)
    for member in (
        None,
        _member("READ_ONLY"),
        _member("READ_WRITE", status=1),
        _member("READ_WRITE", deleted=1),
    ):
        session.members[(TENANT_ID, USER_ID)] = member
        denied = client.post(
            _upload(WORKITEM_ID),
            files=[_file("a.md", b"# A", "text/markdown")],
            headers={"Authorization": "Bearer " + token},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == ErrorCode.NO_PERMISSION.code


def test_one_upload_token_reaches_two_workspaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一用户令牌可以写他有写权限的另一个工单。"""
    session = _ready()
    storage = _client_storage(monkeypatch)
    client = _client(session, monkeypatch, storage)
    token = _mint(session)
    other_id = 50064
    other_tenant = 200
    session.workitems[other_id] = _workitem(other_id, other_tenant)
    session.members[(other_tenant, USER_ID)] = _member("READ_WRITE", tenant=other_tenant)
    first = client.post(
        _upload(WORKITEM_ID),
        files=[_file("first.md", b"# First", "text/markdown")],
        headers={"Authorization": "Bearer " + token},
    )
    second = client.post(
        _upload(other_id),
        files=[_file("second.md", b"# Second", "text/markdown")],
        headers={"Authorization": "Bearer " + token},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert storage.get(_workitem_ref(other_tenant, other_id, "second.md")) == b"# Second"


def test_bad_upload_bytes_return_400_and_duplicates_return_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """格式和魔数不合法是 400。同名文件是 409。"""
    session = _ready()
    client = _client(session, monkeypatch)
    token = _mint(session)
    header = {"Authorization": "Bearer " + token}
    names = (
        "a.exe",
        "fake.png",
        "fake.jpg",
        "fake.jpeg",
        "fake.webp",
        "fake.pdf",
        "fake.zip",
        "fake.docx",
        "fake.doc",
    )
    for name in names:
        rejected = client.post(
            _upload(WORKITEM_ID),
            files=[_file(name, b"not-a-real-file", "application/octet-stream")],
            headers=header,
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == ErrorCode.PARAM_INVALID.code
    for name in ("broken.java", "broken.py"):
        rejected = client.post(
            _upload(WORKITEM_ID),
            files=[_file(name, bytes([0xFF, 0xFE, 0x00, 0x01]), "application/octet-stream")],
            headers=header,
        )
        assert rejected.status_code == 400
    session.artifacts[77] = _artifact(77, WORKITEM_ID, "requirements/spec.md")
    conflict = client.post(
        _upload(WORKITEM_ID),
        files=[_file("spec.md", b"# Spec", "text/markdown")],
        headers=header,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == ErrorCode.CONFLICT.code


def test_download_index_and_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """索引返回元数据，正文按附件和内容类型原样返回。"""
    session = _ready()
    session.members[(TENANT_ID, USER_ID)] = _member("READ_ONLY")
    storage = _client_storage(monkeypatch)
    client = _client(session, monkeypatch, storage)
    payload = b"# Requirements"
    ref = _workitem_ref(TENANT_ID, WORKITEM_ID, "requirements.md")
    storage.put("artifact-bucket", ref.removeprefix("artifact-bucket/"), payload)
    session.artifacts[77] = _artifact(
        77,
        WORKITEM_ID,
        "requirements/requirements.md",
        ref,
        len(payload),
    )
    token = _mint_download(session)
    listed = client.get(_index(WORKITEM_ID), headers={"Authorization": "Bearer " + token})
    assert listed.status_code == 200
    assert listed.json()["data"][0]["id"] == 77
    assert listed.json()["data"][0]["name"] == "requirements/requirements.md"
    assert token not in listed.text
    content = client.get(_content(WORKITEM_ID, 77), headers={"Authorization": "Bearer " + token})
    assert content.status_code == 200
    assert content.headers["content-type"] == "text/markdown"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in content.headers["content-disposition"]
    assert "requirements.md" in content.headers["content-disposition"]
    assert content.content == payload
    image_ref = _workitem_ref(TENANT_ID, WORKITEM_ID, "diagram.png")
    storage.put("artifact-bucket", image_ref.removeprefix("artifact-bucket/"), PNG)
    session.artifacts[78] = _artifact(
        78,
        WORKITEM_ID,
        "requirements/diagram.png",
        image_ref,
        len(PNG),
    )
    image = client.get(_content(WORKITEM_ID, 78), headers={"Authorization": "Bearer " + token})
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == PNG


def test_download_failures_use_controller_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """下载失败按控制器映射状态。正文失败是省略空字段的 JSON。"""
    session = _ready()
    session.members[(TENANT_ID, USER_ID)] = _member("READ_ONLY")
    client = _client(session, monkeypatch)
    expired = DOWNLOAD_PREFIX + sign_user_purpose(USER_ID, DOWNLOAD_PURPOSE, -1)
    wrong = DOWNLOAD_PREFIX + sign_user_purpose(USER_ID, "dispatch-mcp", 1800)
    valid = _mint_download(session)
    tampered = valid[:-4] + "AAAA"
    for header in (
        None,
        "Bearer",
        "Bearer ",
        "Bearer awupload_x",
        "Bearer " + expired,
        "Bearer " + wrong,
        "Bearer " + tampered,
    ):
        kwargs: dict[str, object] = {}
        if header is not None:
            kwargs["headers"] = {"Authorization": header}
        index = client.get(_index(WORKITEM_ID), **kwargs)  # type: ignore[arg-type]
        body = client.get(_content(WORKITEM_ID, 77), **kwargs)  # type: ignore[arg-type]
        assert index.status_code == 401
        assert body.status_code == 401
        assert "data" in index.json()
        assert "data" not in body.json()
        assert body.headers["content-type"] == "application/json"
        assert body.headers["x-content-type-options"] == "nosniff"
        assert valid not in body.text
    session.workitems.clear()
    missing = client.get(_index(WORKITEM_ID), headers={"Authorization": "Bearer " + valid})
    assert missing.status_code == 404
    assert missing.json()["code"] == ErrorCode.WORKITEM_NOT_FOUND.code
    session.workitems[WORKITEM_ID] = _workitem(WORKITEM_ID, TENANT_ID)
    for member in (None, _member("READ_ONLY", status=1), _member("READ_WRITE", deleted=1)):
        session.members[(TENANT_ID, USER_ID)] = member
        denied = client.get(
            _content(WORKITEM_ID, 77),
            headers={"Authorization": "Bearer " + valid},
        )
        assert denied.status_code == 403
    session.members[(TENANT_ID, USER_ID)] = _member("READ_ONLY")
    unknown = client.get(_content(WORKITEM_ID, 999), headers={"Authorization": "Bearer " + valid})
    assert unknown.status_code == 404
    assert unknown.json()["code"] == ErrorCode.ARTIFACT_NOT_FOUND.code
    assert "data" not in unknown.json()
    session.artifacts[77] = _artifact(77, 50999, "requirements/requirements.md")
    foreign = client.get(_content(WORKITEM_ID, 77), headers={"Authorization": "Bearer " + valid})
    assert foreign.status_code == 404
    session.artifacts[77] = _artifact(
        77,
        WORKITEM_ID,
        "requirements/requirements.md",
        _workitem_ref(TENANT_ID, WORKITEM_ID, "absent.md"),
    )
    absent = client.get(_content(WORKITEM_ID, 77), headers={"Authorization": "Bearer " + valid})
    assert absent.status_code == 404


def test_scheduled_task_upload_uses_task_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """定时任务上传写到任务空间，归档是 409，缺失是 404。"""
    session = _ready()
    storage = _client_storage(monkeypatch)
    client = _client(session, monkeypatch, storage)
    token = _mint(session)
    session.tasks[TASK_ID] = _task(TASK_ID, TENANT_ID, "ACTIVE")
    response = client.post(
        _task_upload(TASK_ID),
        files=[_file("requirements.md", b"# Requirements", "text/markdown")],
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200
    assert response.json()["data"][0]["name"] == "requirements/requirements.md"
    assert token not in response.text
    assert storage.get(_task_ref(TENANT_ID, TASK_ID, "requirements.md")) == b"# Requirements"
    audits = [row for row in session.added if isinstance(row, AuditLog)]
    assert audits[-1].action == "UPLOAD_SCHEDULED_TASK_REQUIREMENT_DOC"
    session.tasks[TASK_ID] = _task(TASK_ID, TENANT_ID, "ARCHIVED")
    archived = client.post(
        _task_upload(TASK_ID),
        files=[_file("more.md", b"# More", "text/markdown")],
        headers={"Authorization": "Bearer " + token},
    )
    assert archived.status_code == 409
    assert archived.json()["code"] == ErrorCode.SCHEDULED_TASK_INVALID_STATE.code
    session.tasks.clear()
    missing = client.post(
        _task_upload(TASK_ID),
        files=[_file("more.md", b"# More", "text/markdown")],
        headers={"Authorization": "Bearer " + token},
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == ErrorCode.SCHEDULED_TASK_NOT_FOUND.code


class _Brand:
    def __init__(self, domain: str | None) -> None:
        self.domain = domain


class _Config:
    def __init__(self, base: str, version: str) -> None:
        self.jwt_secret = SECRET
        self.public_base_url = base
        self.recommended_runtime_version = version


class _Buckets:
    oss_artifact_bucket = "artifact-bucket"
    oss_bucket = "base"


class _Cursor:
    lastrowid = 77


class _Session:
    def __init__(self) -> None:
        self.workitems: dict[int, Workitem] = {}
        self.members: dict[tuple[int, int], OrgMember | None] = {}
        self.tasks: dict[int, ScheduledTask] = {}
        self.artifacts: dict[int, Artifact] = {}
        self.branding: _Brand | None = None
        self.added: list[object] = []

    async def scalar(self, statement: object) -> object:
        sql = _bound(statement)
        if "org_member" in sql:
            tenant = _num(sql, "org_member.tenant_id")
            user = _num(sql, "org_member.user_id")
            return self.members.get((tenant, user))
        if "platform_branding_config" in sql:
            return self.branding
        if "scheduled_task" in sql:
            return self.tasks.get(_num(sql, "scheduled_task.id"))
        if "artifact" in sql:
            return self.artifacts.get(_num(sql, "artifact.id"))
        return self.workitems.get(_num(sql, "workitem.id"))

    async def scalars(self, statement: object) -> list[Artifact]:
        sql = _bound(statement)
        workitem_id = _num(sql, "artifact.workitem_id")
        rows = [row for row in self.artifacts.values() if row.workitem_id == workitem_id]
        if "source_type = 'SCHEDULED_TASK'" in sql:
            rows = [row for row in rows if row.source_type == "SCHEDULED_TASK"]
        if "source_type = 'WORKITEM'" in sql:
            rows = [row for row in rows if row.source_type == "WORKITEM"]
        rows.sort(key=lambda row: row.id)
        return rows

    async def execute(self, statement: object) -> _Cursor:
        return _Cursor()

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


def _ready() -> _Session:
    session = _Session()
    session.workitems[WORKITEM_ID] = _workitem(WORKITEM_ID, TENANT_ID)
    session.members[(TENANT_ID, USER_ID)] = _member("READ_WRITE")
    return session


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    base: str = BASE,
    version: str = VERSION,
    storage: InMemoryObjectStorage | None = None,
) -> InMemoryObjectStorage:
    if storage is None:
        storage = InMemoryObjectStorage()
    config = _Config(base, version)
    monkeypatch.setattr("autowonder.security.jwt.get_settings", lambda: config)
    monkeypatch.setattr("autowonder.artifacts.cli_tokens.get_settings", lambda: config)
    monkeypatch.setattr("autowonder.artifacts.documents.get_settings", lambda: _Buckets())
    monkeypatch.setattr("autowonder.artifacts.documents.get_object_storage", lambda: storage)
    return storage


def _client_storage(monkeypatch: pytest.MonkeyPatch) -> InMemoryObjectStorage:
    return _patch(monkeypatch)


def _client(
    session: _Session,
    monkeypatch: pytest.MonkeyPatch,
    storage: InMemoryObjectStorage | None = None,
) -> TestClient:
    _patch(monkeypatch, storage=storage)
    app = create_app()

    async def override() -> object:
        yield session

    app.dependency_overrides[get_session] = override
    return TestClient(app)


def _mint(session: _Session) -> str:
    return _mint_upload(session)


def _mint_upload(session: _Session) -> str:
    view = asyncio.run(mint_upload_token(session, CredentialType.LONG_LIVED, USER_ID, WORKITEM_ID))
    return view.token


def _mint_download(session: _Session) -> str:
    view = asyncio.run(
        mint_download_token(session, CredentialType.LONG_LIVED, USER_ID, WORKITEM_ID)
    )
    return view.token


def _file(name: str, payload: bytes, content_type: str) -> tuple[str, tuple[str, bytes, str]]:
    return ("files", (name, payload, content_type))


def _upload(workitem_id: int) -> str:
    return "/api/cli/workitems/" + str(workitem_id) + "/requirement-documents"


def _index(workitem_id: int) -> str:
    return _upload(workitem_id) + "/index"


def _content(workitem_id: int, artifact_id: int) -> str:
    return _upload(workitem_id) + "/" + str(artifact_id) + "/content"


def _task_upload(task_id: int) -> str:
    return "/api/cli/scheduled-tasks/" + str(task_id) + "/documents"


def _workitem_ref(tenant_id: int, workitem_id: int, filename: str) -> str:
    return (
        "artifact-bucket/t/"
        + str(tenant_id)
        + "/workitem/"
        + str(workitem_id)
        + "/requirements/"
        + filename
    )


def _task_ref(tenant_id: int, task_id: int, filename: str) -> str:
    return (
        "artifact-bucket/t/"
        + str(tenant_id)
        + "/scheduled-task/"
        + str(task_id)
        + "/requirements/"
        + filename
    )


def _workitem(workitem_id: int, tenant_id: int) -> Workitem:
    moment = datetime(2026, 1, 2, 3, 4, 5)
    return Workitem(
        id=workitem_id,
        tenant_id=tenant_id,
        work_type="REQ",
        title="需求",
        gmt_create=moment,
        gmt_modified=moment,
    )


def _member(
    level: str,
    status: int = 0,
    deleted: int = 0,
    tenant: int = TENANT_ID,
    user: int = USER_ID,
) -> OrgMember:
    moment = datetime(2026, 1, 2, 3, 4, 5)
    return OrgMember(
        tenant_id=tenant,
        user_id=user,
        status=status,
        access_level=level,
        is_deleted=deleted,
        gmt_create=moment,
        gmt_modified=moment,
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
        creator_id=USER_ID,
        gmt_create=moment,
        gmt_modified=moment,
    )


def _artifact(
    artifact_id: int,
    workitem_id: int,
    name: str,
    oss_ref: str = "artifact-bucket/missing",
    size: int = 6,
    source_type: str = "WORKITEM",
    tenant_id: int = TENANT_ID,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        tenant_id=tenant_id,
        source_type=source_type,
        workitem_id=workitem_id,
        name=name,
        type=TYPE,
        oss_ref=oss_ref,
        size=size,
        gmt_create=datetime(2026, 1, 2, 3, 4, 5),
    )


def _bound(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled).replace("`", "")


def _num(sql: str, column: str) -> int:
    match = re.search(re.escape(column) + r" = (\d+)", sql)
    assert match is not None
    return int(match.group(1))
