"""产物列表去重、下载地址和预览内容类型。这些检查不访问数据库。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from starlette.responses import Response

from autowonder.artifacts.models import Artifact
from autowonder.artifacts.router import preview_artifact
from autowonder.artifacts.service import (
    MAX_PREVIEW_BYTES,
    content_type,
    download_url,
    file_extension,
    find_by_id_statement,
    is_html,
    list_by_dispatch_statement,
    list_by_workitem_statement,
    logical_name,
    preview_status,
    read_preview,
    visible_dispatch_views,
    visible_workitem_views,
)
from autowonder.core.context import RequestContext, reset_context, set_context
from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app


def test_workitem_list_drops_telemetry_and_keeps_latest_logical_name() -> None:
    """观测文件不出现。同一逻辑名只保留先出现的一条。"""
    rows = [
        _artifact(id=4, name="observability/context/files/hash"),
        _artifact(id=3, name="artifacts/output/deliverables/report.md"),
        _artifact(id=2, name="artifacts/output/evidence/test.log"),
        _artifact(id=1, name="artifacts/output/deliverables/report.md"),
    ]
    views = visible_workitem_views(rows)
    assert [view.id for view in views] == [3, 2]
    assert logical_name("artifacts/output/deliverables/report.md") == "deliverables/report.md"
    assert logical_name("output/deliverables/report.md") == "deliverables/report.md"
    assert logical_name(None) == ""


def test_same_name_kept_across_dispatches_and_alias_dropped_within_one() -> None:
    """跨派发保留同名文件。同一派发里 output 别名被丢掉。"""
    latest = _artifact(id=4, dispatch_id=20, name="artifacts/output/deliverables/report.md")
    alias = _artifact(id=3, dispatch_id=20, name="output/deliverables/report.md")
    historical = _artifact(id=2, dispatch_id=10, name=latest.name)
    views = visible_workitem_views([latest, alias, historical])
    assert [view.id for view in views] == [4, 2]
    unnamed = _artifact(id=8, dispatch_id=None, name="notes.txt")
    assert visible_workitem_views([unnamed])[0].dispatch_id is None


def test_classification_on_read_does_not_rewrite_stored_type() -> None:
    """FILE 按路径补展示类型，登记行上的类型保持 FILE。"""
    snapshot = _artifact(
        id=1,
        name="artifacts/attempts/step-1/attempt-2/deliverables/report.md",
        type="FILE",
    )
    result = _artifact(id=2, name="result/runtime-result.json", type="FILE")
    workitem = visible_workitem_views([result, snapshot])
    dispatch = visible_dispatch_views([result, snapshot])
    assert [view.type for view in workitem] == ["RUNTIME", "SNAPSHOT"]
    assert [view.type for view in dispatch] == ["RUNTIME", "SNAPSHOT"]
    assert snapshot.type == "FILE"
    assert result.type == "FILE"


def test_dispatch_list_keeps_duplicate_names() -> None:
    """派发详情不去重，只丢掉观测文件。"""
    first = _artifact(id=2, name="artifacts/output/deliverables/report.md")
    second = _artifact(id=1, name="output/deliverables/report.md")
    hidden = _artifact(id=9, name="output/observability/trace.json")
    views = visible_dispatch_views([first, second, hidden])
    assert [view.id for view in views] == [2, 1]


def test_extension_strips_query_and_fragment() -> None:
    """扩展名取最后一个点，并忽略查询串与片段。"""
    assert file_extension("requirements/PROTOTYPE.HTM?download=1#top") == "htm"
    assert file_extension("requirements/PLAN.HTML") == "html"
    assert file_extension("archive") == ""
    assert file_extension(None) == ""
    assert content_type("artifacts/output/report.md") == "text/markdown;charset=UTF-8"
    assert content_type("artifacts/output/demo.mp4") == "video/mp4"
    assert content_type("notes.txt") == "text/plain"
    assert content_type("archive.zip") == "application/octet-stream"
    assert is_html("requirements/plan.html") is True
    assert is_html("requirements/PROTOTYPE.HTM") is True
    assert is_html("artifacts/output/report.md") is False
    assert preview_status(ErrorCode.UNAUTHORIZED.code) == 401
    assert preview_status(ErrorCode.ARTIFACT_NOT_FOUND.code) == 404
    assert preview_status(ErrorCode.PARAM_INVALID.code) == 400


async def test_download_returns_presigned_url_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """http 与 https 预签名地址原样返回，有效期 600 秒。"""
    row = _artifact(id=1, oss_ref="b/k")
    storage = _Storage("http://172.19.133.124:9000/b/k?X-Amz-Signature=s")
    monkeypatch.setattr("autowonder.artifacts.service.get_object_storage", lambda: storage)
    url = await download_url(_Session(row), 1, 100)
    assert url == "http://172.19.133.124:9000/b/k?X-Amz-Signature=s"
    assert storage.presigns == [("b/k", 600)]
    storage.url = "https://172.19.133.124:9000/b/k?X-Amz-Signature=s"
    assert await download_url(_Session(row), 1, 100) == storage.url


async def test_download_missing_or_wrong_tenant_does_not_presign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺失或其他工作空间是 17010，并且不签发地址。"""
    storage = _Storage("https://signed/b/k")
    monkeypatch.setattr("autowonder.artifacts.service.get_object_storage", lambda: storage)
    with pytest.raises(BizError) as missing:
        await download_url(_Session(None), 9, 100)
    assert missing.value.code == ErrorCode.ARTIFACT_NOT_FOUND.code
    other = _artifact(id=1, tenant_id=100, oss_ref="b/k")
    with pytest.raises(BizError) as wrong:
        await download_url(_Session(other), 1, 999)
    assert wrong.value.code == "17010"
    assert storage.presigns == []


def test_preview_reads_allowed_types_and_skips_storage_otherwise() -> None:
    """可预览类型读取正文。超限、未知大小和压缩包不访问存储。"""
    storage = _Storage("")
    storage.payload = b"# Report"
    markdown = _artifact(id=1, name="artifacts/output/report.md", size=100, oss_ref="b/k")
    name, payload = read_preview(markdown, storage)
    assert name == "artifacts/output/report.md"
    assert payload == b"# Report"
    storage.payload = bytes([0, 1, 2])
    video = _artifact(id=1, name="artifacts/output/demo.mp4", size=1024, oss_ref="b/k")
    assert read_preview(video, storage)[1] == bytes([0, 1, 2])
    storage.payload = b"<html></html>"
    html = _artifact(id=1, name="requirements/plan.html", size=100, oss_ref="b/k")
    assert read_preview(html, storage) == ("requirements/plan.html", b"<html></html>")
    upper = _artifact(id=2, name="requirements/PLAN.HTML", size=100, oss_ref="b/html")
    assert read_preview(upper, storage)[0] == "requirements/PLAN.HTML"
    exact = _artifact(
        id=1,
        name="requirements/exact.html",
        size=MAX_PREVIEW_BYTES,
        oss_ref="b/exact",
    )
    storage.gets.clear()
    read_preview(exact, storage)
    assert storage.gets == ["b/exact"]
    before = len(storage.gets)
    large = _artifact(
        id=1,
        name="requirements/big.html",
        size=MAX_PREVIEW_BYTES + 1,
        oss_ref="b/k",
    )
    _reject_preview(large, storage)
    unknown = _artifact(id=1, name="artifacts/output/report.md", size=None, oss_ref="b/k")
    _reject_preview(unknown, storage)
    archive = _artifact(id=1, name="artifacts/output/archive.zip", size=10, oss_ref="b/k")
    _reject_preview(archive, storage)
    assert len(storage.gets) == before
    storage.payload = None
    missing = _artifact(id=1, name="artifacts/output/report.md", size=4, oss_ref="b/missing")
    with pytest.raises(BizError) as caught:
        read_preview(missing, storage)
    assert caught.value.code == ErrorCode.ARTIFACT_NOT_FOUND.code


async def test_preview_response_sets_content_type_and_sandbox_only_for_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Markdown 无 CSP。html 与 htm 带 sandbox，失败体是纯文本。"""
    token = set_context(RequestContext(workspace_id=100, user_id=1, access_level="READ_ONLY"))
    try:
        markdown = await _preview(monkeypatch, "artifacts/output/report.md", b"# Report")
        assert markdown.status_code == 200
        assert markdown.headers["content-type"] == "text/markdown;charset=UTF-8"
        assert markdown.body == b"# Report"
        assert "content-security-policy" not in markdown.headers
        assert markdown.headers["x-content-type-options"] == "nosniff"
        video = await _preview(monkeypatch, "artifacts/output/demo.mp4", bytes([0, 1, 2]))
        assert video.headers["content-type"] == "video/mp4"
        assert "content-security-policy" not in video.headers
        html = await _preview(
            monkeypatch,
            "requirements/plan.html",
            b"<html><body>plan</body></html>",
        )
        assert html.headers["content-type"] == "text/html;charset=UTF-8"
        assert html.headers["content-security-policy"] == "sandbox"
        htm = await _preview(monkeypatch, "requirements/PROTOTYPE.HTM", b"<html></html>")
        assert htm.headers["content-type"] == "text/html;charset=UTF-8"
        assert htm.headers["content-security-policy"] == "sandbox"
        upper = await _preview(monkeypatch, "requirements/PLAN.HTML", b"<html></html>")
        assert upper.headers["content-security-policy"] == "sandbox"
        missing = await _preview_error(monkeypatch, BizError(ErrorCode.ARTIFACT_NOT_FOUND))
        assert missing.status_code == 404
        assert missing.headers["content-type"] == "text/plain"
        assert missing.headers["x-content-type-options"] == "nosniff"
        assert missing.body == "产物不存在".encode()
        invalid = await _preview_error(monkeypatch, BizError(ErrorCode.PARAM_INVALID))
        assert invalid.status_code == 400
        assert invalid.body == "参数不合法".encode()
    finally:
        reset_context(token)


def test_list_sql_matches_source_aware_dao() -> None:
    """工单列表限定 WORKITEM。按主键读取不带租户条件。"""
    workitem = _sql(list_by_workitem_statement(100, 3))
    assert "artifact.tenant_id = 100" in workitem
    assert "artifact.source_type = 'WORKITEM'" in workitem
    assert "artifact.workitem_id = 3" in workitem
    assert "ORDER BY artifact.id DESC" in workitem
    dispatch = _sql(list_by_dispatch_statement(100, 10))
    assert "artifact.tenant_id = 100" in dispatch
    assert "artifact.dispatch_id = 10" in dispatch
    assert "AND artifact.source_type" not in dispatch
    assert "ORDER BY artifact.id DESC" in dispatch
    found = _sql(find_by_id_statement(1))
    assert "artifact.id = 1" in found
    assert "AND artifact.tenant_id" not in found
    assert "LIMIT 1" in found


def test_artifact_routes_require_login() -> None:
    """产物和需求文档接口已注册。未登录返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "get" in paths["/api/workitems/{id}/artifacts"]
    assert "get" in paths["/api/artifacts/{id}/download"]
    assert "get" in paths["/api/artifacts/{id}/preview"]
    documents = paths["/api/workitems/{id}/requirement-documents"]
    assert "get" in documents
    assert "post" in documents
    removed = paths["/api/workitems/{id}/requirement-documents/{artifactId}"]
    assert "delete" in removed
    listed = client.get("/api/workitems/3/artifacts")
    assert listed.status_code == 401
    assert listed.json()["code"] == "10401"
    download = client.get("/api/artifacts/1/download")
    assert download.status_code == 401
    preview = client.get("/api/artifacts/1/preview")
    assert preview.status_code == 401
    documents = client.get("/api/workitems/3/requirement-documents")
    assert documents.status_code == 401
    assert documents.json()["code"] == "10401"


def _reject_preview(row: Artifact, storage: "_Storage") -> None:
    with pytest.raises(BizError) as caught:
        read_preview(row, storage)
    assert caught.value.code == ErrorCode.PARAM_INVALID.code


def _sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)


def _artifact(**values: object) -> Artifact:
    fields: dict[str, object] = {
        "tenant_id": 100,
        "source_type": "WORKITEM",
        "workitem_id": 3,
        "name": "report.md",
        "type": "LOG",
        "oss_ref": "b/k",
        "gmt_create": datetime(2026, 1, 2, 3, 4, 5),
    }
    fields.update(values)
    return Artifact(**fields)  # type: ignore[arg-type]


class _Storage:
    """记录读取和签发，地址由测试指定。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.payload: bytes | None = b""
        self.gets: list[str] = []
        self.presigns: list[tuple[str, int]] = []

    def get(self, oss_ref: str) -> bytes | None:
        self.gets.append(oss_ref)
        return self.payload

    def presign_get(self, oss_ref: str, ttl_seconds: int) -> str:
        self.presigns.append((oss_ref, ttl_seconds))
        return self.url


class _Session:
    """按主键查询只返回预先放好的一行。"""

    def __init__(self, row: Artifact | None) -> None:
        self.row = row

    async def scalar(self, statement: object) -> Artifact | None:
        return self.row


async def _preview(monkeypatch: pytest.MonkeyPatch, name: str, payload: bytes) -> Response:
    async def succeed(session: object, artifact_id: int, workspace_id: int) -> tuple[str, bytes]:
        assert artifact_id == 7
        assert workspace_id == 100
        return name, payload

    monkeypatch.setattr("autowonder.artifacts.router.preview_bytes", succeed)
    return await preview_artifact(7, _Session(None))  # type: ignore[arg-type]


async def _preview_error(monkeypatch: pytest.MonkeyPatch, error: BizError) -> Response:
    async def fail(session: object, artifact_id: int, workspace_id: int) -> tuple[str, bytes]:
        raise error

    monkeypatch.setattr("autowonder.artifacts.router.preview_bytes", fail)
    return await preview_artifact(7, _Session(None))  # type: ignore[arg-type]
