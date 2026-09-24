"""调试日志的查询窗口、下载地址、列值整理和对象名。这些检查不访问数据库。"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data
from autowonder.debuglogs.models import DebugLog
from autowonder.debuglogs.naming import (
    sanitize_role_code,
    scheduled_object_key,
    workitem_object_key,
)
from autowonder.debuglogs.sanitizer import (
    accepted_upload_channel,
    logged_channel,
    sanitize_sha256,
    truncate_error_message,
)
from autowonder.debuglogs.service import (
    artifact_bucket,
    download_url,
    logs_select,
    query_window,
    require_query_source_type,
    since_local,
    to_view,
)
from autowonder.main import create_app

_HEX = "a" * 64
_EMOJI = "\U0001f916"


def test_query_window_matches_java_paging() -> None:
    """page 小于 1 从第一页开始，size 小于 1 用 50，超过 200 截断。"""
    assert query_window(1, 50) == (50, 0)
    assert query_window(0, 1000) == (200, 0)
    assert query_window(3, 50) == (50, 100)
    assert query_window(-4, 0) == (50, 0)
    assert query_window(2, 200) == (200, 200)


def test_query_source_rejects_task_definition_and_unknown() -> None:
    """SCHEDULED_TASK 和未知来源都是参数不合法。"""
    assert require_query_source_type("WORKITEM") == "WORKITEM"
    assert require_query_source_type("SCHEDULED_TASK_RUN") == "SCHEDULED_TASK_RUN"
    with pytest.raises(BizError) as task_error:
        require_query_source_type("SCHEDULED_TASK")
    assert task_error.value.code == ErrorCode.PARAM_INVALID.code
    with pytest.raises(BizError) as unknown_error:
        require_query_source_type("NOPE")
    assert unknown_error.value.code == ErrorCode.PARAM_INVALID.code


def test_logs_select_filters_orders_and_clamps_page() -> None:
    """可选条件缺省时不进 SQL。第三页 50 条的偏移是 100。"""
    plain = _sql(logs_select(100, "WORKITEM", 200, None, None, 3, 50))
    assert "debug_log.tenant_id = 100" in plain
    assert "debug_log.source_type = " in plain
    assert "debug_log.source_id = 200" in plain
    assert "AND debug_log.agent_id" not in plain
    assert "AND debug_log.gmt_create" not in plain
    assert "ORDER BY debug_log.run_no ASC, debug_log.id ASC" in plain
    assert "LIMIT 100, 50" in plain
    filtered = _sql(logs_select(100, "SCHEDULED_TASK_RUN", 77, 400, 1_700_000_000_000, 0, 1000))
    assert "debug_log.agent_id = 400" in filtered
    assert "debug_log.gmt_create >= " in filtered
    assert since_local(1_700_000_000_000) == datetime(2023, 11, 15, 6, 13, 20)
    assert "LIMIT 0, 200" in filtered


def test_download_url_presigns_uploaded_rows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """PENDING 和 FAILED 不签发。UPLOADED 用产物桶和 600 秒。"""
    calls: list[tuple[str, int]] = []

    class _Storage:
        def presign_get(self, oss_ref: str, ttl_seconds: int) -> str:
            calls.append((oss_ref, ttl_seconds))
            return "https://oss/get?sig=1"

    monkeypatch.setattr(
        "autowonder.debuglogs.service.get_settings",
        lambda: type("Settings", (), {"oss_artifact_bucket": "test-artifact-bucket"})(),
    )
    monkeypatch.setattr(
        "autowonder.debuglogs.service.get_object_storage",
        lambda: _Storage(),
    )
    pending = _row("PENDING")
    failed = _row("FAILED")
    uploaded = _row("UPLOADED")
    assert download_url(pending) is None
    assert download_url(failed) is None
    assert download_url(uploaded) == "https://oss/get?sig=1"
    assert calls == [
        ("test-artifact-bucket/debug/200/DevAgent-run-2.log.gz", 600),
    ]


def test_artifact_bucket_falls_back_only_when_unset() -> None:
    """空字符串保留，只有 None 才回到日常桶。"""
    assert artifact_bucket(None) == "autowonder-artifact-daily"
    assert artifact_bucket("") == ""
    assert artifact_bucket("artifacts") == "artifacts"


def test_view_keeps_provisional_status_and_uploaded_error_note() -> None:
    """非终态调度状态和已上传行的收尾注记原样出现在结果里。"""
    row = _row("UPLOADED", dispatch_status="RUNNING", error_message="writer close note")
    row.upload_channel = "RELAY"
    row.truncated = 0
    body = dump_data(to_view(row, "https://oss/get?sig=3"))
    assert body["dispatchStatus"] == "RUNNING"
    assert body["errorMessage"] == "writer close note"
    assert body["uploadChannel"] == "RELAY"
    assert body["downloadUrl"] == "https://oss/get?sig=3"
    assert body["truncated"] is False
    assert body["objectKey"] == "debug/200/DevAgent-run-2.log.gz"
    assert body["runNo"] == 2
    assert body["sizeBytes"] == 2048
    assert "agentVersionId" not in body


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (_HEX, _HEX),
        ("A" * 64, "A" * 64),
        ("sha256:" + "b" * 64, "b" * 64),
        ("z" * 120, "z" * 80),
        ("abc\ndef", "abc-def"),
        (None, None),
        ("   ", None),
        ("sha256:zzz", "sha256:zzz"),
    ],
)
def test_sha256_sanitizer_matches_java_vectors(reported: str | None, expected: str | None) -> None:
    """sha256 的透传、剥前缀、截断和空白规则与 Java 向量一致。"""
    assert sanitize_sha256(reported) == expected


def test_astral_sha256_truncates_by_code_point() -> None:
    """100 个补充平面字符截到 80 个，末尾仍是完整字符。"""
    value = sanitize_sha256(_EMOJI * 100)
    assert value is not None
    assert len(value) == 80
    assert ord(value[-1]) == 0x1F916


@pytest.mark.parametrize(
    ("role_code", "expected"),
    [
        ("Dev Agent", "Dev-Agent"),
        ("Dev/Agent", "Dev-Agent"),
        ("QA_1-x", "QA_1-x"),
        ("  Dev  ", "Dev"),
        ("\u3000Dev\u3000", "Dev"),
        (None, "agent-400"),
        ("   ", "agent-400"),
        ("\u3000\u3000", "agent-400"),
        ("\u00a0Dev\u00a0", "-Dev-"),
        ("中文", "--"),
        (_EMOJI, "--"),
        ("Dev" + _EMOJI + "x", "Dev--x"),
        ("a" * 90, "a" * 64),
        ("b" * 63 + "中" + "tail", "b" * 63 + "-"),
        ("c" * 64 + _EMOJI, "c" * 64),
    ],
)
def test_role_code_matches_java_vectors(role_code: str | None, expected: str) -> None:
    """roleCode 归一与 Java UTF-16 向量一致，不换行空格不会被当成空白。"""
    assert sanitize_role_code(role_code, 400) == expected


def test_object_keys_follow_the_protocol() -> None:
    """工单和定时任务的对象键格式固定。"""
    assert workitem_object_key(200, "DevAgent", 2) == "debug/200/DevAgent-run-2.log.gz"
    assert (
        scheduled_object_key(8, 77, "DevAgent", 1)
        == "debug/scheduled-8-run-77/DevAgent-run-1.log.gz"
    )


def test_error_message_truncates_by_code_point() -> None:
    """1024 个 code point 保留，再多一个就截断，补充平面字符算一个。"""
    assert truncate_error_message(None) is None
    assert truncate_error_message("ok") == "ok"
    assert truncate_error_message("x" * 1024) == "x" * 1024
    assert truncate_error_message("x" * 1025) == "x" * 1024
    assert truncate_error_message(_EMOJI * 1025) == _EMOJI * 1024


def test_bad_channel_is_dropped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非法 channel 不入库，日志里的控制字符换成横线。"""
    assert accepted_upload_channel(900, None) is None
    assert accepted_upload_channel(900, "DIRECT") == "DIRECT"
    assert accepted_upload_channel(900, "RELAY") == "RELAY"
    with caplog.at_level("WARNING"):
        assert accepted_upload_channel(900, "RELAY\nX\rY\tZ") is None
    assert "dispatchId=900" in caplog.text
    assert "channel=RELAY-X-Y-Z" in caplog.text
    assert "reason=DEBUG_LOG_REPORT_BAD_CHANNEL" in caplog.text
    assert "\nX" not in caplog.text
    assert logged_channel(_EMOJI * 40) == _EMOJI * 32


def test_debug_log_routes_match_java_and_require_login() -> None:
    """查询路径要求登录。直传签发已注册，并且不走会话 JWT。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "get" in paths["/api/debug-logs"]
    assert "post" not in paths["/api/debug-logs"]
    upload_path = "/api/daemon/dispatches/{dispatchId}/debug-log-upload"
    assert "post" in paths[upload_path]
    response = client.get("/api/debug-logs", params={"sourceType": "WORKITEM", "sourceId": 1})
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    issued = client.post("/api/daemon/dispatches/1/debug-log-upload")
    assert issued.status_code != 401


def _sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)


def _row(status: str, **overrides: object) -> DebugLog:
    values: dict[str, object] = {
        "id": 2,
        "tenant_id": 100,
        "source_type": "WORKITEM",
        "source_id": 200,
        "dispatch_id": 902,
        "agent_id": 400,
        "run_no": 2,
        "dispatch_status": "SUCCEEDED",
        "object_key": "debug/200/DevAgent-run-2.log.gz",
        "size_bytes": 2048,
        "truncated": 0,
        "status": status,
        "gmt_create": datetime(2026, 1, 2, 3, 4, 5),
        "gmt_modified": datetime(2026, 1, 2, 3, 4, 5),
    }
    values.update(overrides)
    return DebugLog(**values)  # type: ignore[arg-type]
