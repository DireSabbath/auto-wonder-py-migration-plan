"""执行器用户接口里不访问数据库的规则，以及路径和未登录响应。"""

import base64
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.executors.catalog import parse_catalog_snapshot, require_supported_provider
from autowonder.executors.issue import issue_executor_token
from autowonder.executors.launch import build_launch_command, build_ws_url, debug_log_file_name
from autowonder.executors.options import (
    FALLBACK_MODELS,
    choose_model,
    require_creatable_client_kind,
    resolve_max_concurrent,
    resolve_memory_mode,
)
from autowonder.executors.restart import apply_restart_timeout
from autowonder.executors.router import router
from autowonder.executors.tokens import resolve, validate
from autowonder.executors.version import compare_versions, is_behind
from autowonder.main import create_app

_BASE = "https://auto-wonder.example.com"
_RUNTIME = "0.2.152"
_WS = "wss://auto-wonder.example.com/ws/executor"
_NOW = datetime(2026, 9, 4, 13, 45, 0)


def _command(
    token: str | None = "awexec_plain",
    client_kind: str | None = "QODER_CLI",
    memory_mode: str | None = "platform",
    model: str | None = "qmodel_latest",
    reasoning_effort: str | None = "medium",
    context_window: str | None = "260000",
    os_name: str | None = "posix",
    debug: bool = False,
    shell: str | None = None,
    now: datetime = _NOW,
    max_concurrent: int = 5,
    public_base_url: str | None = _BASE,
) -> str:
    return build_launch_command(
        token,
        9,
        client_kind,
        memory_mode,
        model,
        reasoning_effort,
        context_window,
        os_name,
        debug,
        shell,
        now,
        max_concurrent,
        public_base_url,
        _RUNTIME,
    ).command


def test_version_token_catalog_and_launch_rules() -> None:
    """版本按数字比较；令牌可回显；目录快照和启动命令跟 Java 的纯规则一致。"""
    assert is_behind("0.2.9", "0.2.152") is True
    assert compare_versions(" v1.2.3 ", "1.2.3") == 0
    assert compare_versions("1.2.3-beta", "1.2.3") is None
    assert is_behind("latest", "1.2.3") is False
    assert compare_versions("9999999999999999999.0.0", "1.0.0") is None

    plaintext, token_ref = issue_executor_token(42)
    assert plaintext != token_ref
    assert token_ref.startswith("b64:")
    assert resolve(token_ref) == plaintext
    assert validate(token_ref, plaintext) is True
    assert validate(token_ref, plaintext + "x") is False
    assert issue_executor_token(1)[0] != issue_executor_token(1)[0]

    snapshot = (
        '{"provider":"qoder","models":[{"id":"first","name":"First model"},'
        '{"id":"second","name":"Second model"}],"sourceExecutorId":7,'
        '"lastSuccessfulAt":1725177600123}'
    )
    parsed = parse_catalog_snapshot("qoder", snapshot)
    assert parsed is not None
    assert [item.id for item in parsed.models] == ["first", "second"]
    assert parse_catalog_snapshot("qoder", "not-json") is None
    assert parse_catalog_snapshot("qoder", "  ") is None
    wrong_provider = '{"provider":"qodercn","models":[],"sourceExecutorId":7,"lastSuccessfulAt":1}'
    assert parse_catalog_snapshot("qoder", wrong_provider) is None
    try:
        require_supported_provider("claude")
    except BizError as error:
        assert error.error_code is ErrorCode.PARAM_INVALID
        assert str(error) == "仅支持 qoder 或 qodercn provider"
    else:
        raise AssertionError("expected unsupported provider")

    try:
        require_creatable_client_kind("CLAUDE_CODE", ErrorCode.EXECUTOR_CLIENT_KIND_INVALID)
    except BizError as error:
        assert error.error_code is ErrorCode.EXECUTOR_CLIENT_KIND_INVALID
        assert str(error) == "clientKind 仅支持 QODER_CLI/QODER_CN_CLI"
    else:
        raise AssertionError("expected rejected client kind")
    canonical = require_creatable_client_kind(
        " qoder_cli ",
        ErrorCode.EXECUTOR_CLIENT_KIND_INVALID,
    )
    assert canonical == "QODER_CLI"
    assert resolve_memory_mode(None) == "platform"
    assert resolve_max_concurrent(None) == 5
    try:
        resolve_max_concurrent(11)
    except BizError as error:
        assert str(error) == "最大并发任务数必须为 1 到 10 的整数"
    else:
        raise AssertionError("expected rejected concurrency")
    assert choose_model(FALLBACK_MODELS, "qmodel_latest") == "qmodel_latest"
    without_preferred = tuple(item for item in FALLBACK_MODELS if item.value != "qmodel_latest")
    assert choose_model(without_preferred, "qmodel_latest") == "auto"

    posix = _command()
    assert posix == (
        "npx -y autowonder@" + _RUNTIME + " connect"
        " --ws-url " + _WS + " --token awexec_plain"
        " --executor-id 9"
        " --provider qoder"
        " --memory-mode platform --max-tasks 5"
        " --model qmodel_latest"
        " --reasoning-effort medium"
        " --context-window 260000"
        " --token-aware-enable"
    )
    quoted = _command(token="aw exec'token")
    assert "--token 'aw exec'\\''token'" in quoted
    encoded = _command(os_name="windows")
    assert encoded.startswith("powershell -NoProfile -EncodedCommand ")
    payload = encoded.removeprefix("powershell -NoProfile -EncodedCommand ")
    script = base64.b64decode(payload).decode("utf-16-le")
    assert script.endswith(" --token-aware-enable")
    assert " --provider qoder " in script
    assert build_ws_url("  HTTPS://Auto-Wonder.Example.COM:443/api/mcp  ") == _WS
    assert build_ws_url("http://localhost:8080") == "ws://localhost:8080/ws/executor"
    assert debug_log_file_name("QODER_CN_CLI", 12, _NOW) == "aw-qodercn-12-260904-13-45-00.log"
    try:
        _command(client_kind="  ")
    except BizError as error:
        assert error.error_code is ErrorCode.EXECUTOR_CLIENT_KIND_MISSING
    else:
        raise AssertionError("expected missing client kind")
    try:
        _command(os_name="macos")
    except BizError as error:
        assert error.error_code is ErrorCode.MCP_TOOL_ARGUMENT_INVALID
        assert "os 仅支持 posix/windows" in str(error)
    else:
        raise AssertionError("expected rejected os")
    try:
        build_ws_url("ftp://auto-wonder.example.com")
    except BizError as error:
        assert error.error_code is ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected rejected mcp url")

    issued = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    state = {
        "status": "REQUESTED",
        "issuedAt": "2026-09-24T00:00:00Z",
        "message": "已发送，等待客户端响应",
    }
    still_waiting = apply_restart_timeout(dict(state), issued + timedelta(seconds=600))
    assert still_waiting["status"] == "REQUESTED"
    timed_out = apply_restart_timeout(dict(state), issued + timedelta(seconds=601))
    assert timed_out["status"] == "TIMED_OUT"
    assert timed_out["message"] == "未收到重启后的心跳，请检查客户端"
    done = {"status": "COMPLETED", "issuedAt": "2026-09-24T00:00:00Z"}
    assert apply_restart_timeout(done, issued + timedelta(seconds=601))["status"] == "COMPLETED"


def test_executor_routes_match_java_and_require_login() -> None:
    """路径和方法与 Java 一致，未带令牌时返回 401。"""
    app = create_app()
    app.include_router(router)
    client = TestClient(app)
    paths = client.app.openapi()["paths"]
    expected = {
        "/api/agents/{agentId}/executors": {"post", "get"},
        "/api/executors": {"get"},
        "/api/executor-model-catalogs/{provider}": {"get"},
        "/api/executors/update-all": {"post"},
        "/api/executors/{id}/token": {"get"},
        "/api/executors/{id}/launch-config": {"get", "put"},
        "/api/executors/{id}/launch-command": {"post"},
        "/api/executors/{id}": {"delete"},
        "/api/executors/{id}/restart": {"post"},
        "/api/executors/{id}/update": {"post"},
    }
    for path, methods in expected.items():
        assert path in paths
        assert methods <= set(paths[path])
    route_paths = [getattr(route, "path", "") for route in client.app.routes]
    update_all_index = route_paths.index("/api/executors/update-all")
    item_index = route_paths.index("/api/executors/{id}")
    assert update_all_index < item_index
    response = client.get("/api/executors")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    catalog = client.get("/api/executor-model-catalogs/qoder")
    assert catalog.status_code == 401
    assert catalog.json()["code"] == "10401"
