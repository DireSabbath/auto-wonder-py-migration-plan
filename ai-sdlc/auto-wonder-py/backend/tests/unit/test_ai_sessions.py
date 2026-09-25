"""AI 会话路径，以及 CLI 解析和结果校验。"""

from fastapi.testclient import TestClient

from autowonder.ai.adapters import validate_result
from autowonder.ai.cli_executor import CliExecutor, extract_json_block, parse_stream_output
from autowonder.ai.router import router as ai_router
from autowonder.ai.session import CreateSessionRequest, should_persist_initial_user_message
from autowonder.main import create_app

_PATHS = {
    "/api/ai/sessions": {"post"},
    "/api/ai/sessions/{id}": {"get"},
    "/api/ai/sessions/{id}/messages": {"post"},
    "/api/ai/sessions/{id}/confirm": {"post"},
    "/api/ai/sessions/{id}/cancel": {"post"},
}


def _client() -> TestClient:
    app = create_app()
    app.include_router(ai_router)
    return TestClient(app)


def test_ai_session_routes_exist() -> None:
    """五个会话端点都注册了。"""
    paths = _client().app.openapi()["paths"]
    for path, methods in _PATHS.items():
        assert methods <= set(paths[path])


def test_ai_session_routes_require_login() -> None:
    """未带令牌时返回 401，业务码 10401。"""
    client = _client()
    created = client.post("/api/ai/sessions", json={"scene": "CLARIFICATION"})
    assert created.status_code == 401
    assert created.json()["code"] == "10401"
    loaded = client.get("/api/ai/sessions/1")
    assert loaded.status_code == 401
    assert loaded.json()["code"] == "10401"


def test_stream_json_extracts_text_and_session() -> None:
    """assistant 文本和 result 会话号会留下，代码块里的 JSON 被抽出。"""
    parsed = parse_stream_output(
        [
            "",
            (
                '{"type":"assistant","message":{"content":['
                '{"type":"text","text":"```json\\n{\\"ok\\":1}\\n```"}]}}'
            ),
            '{"type":"result","session_id":"sess-1","result":"ignored"}',
        ]
    )
    assert parsed.cli_session_id == "sess-1"
    assert extract_json_block(parsed.text) == '{"ok":1}'


def test_cli_command_direct_and_shell() -> None:
    """direct 直接执行参数，其他模式套一层 shell。"""
    direct = CliExecutor("claude", 30).build_command("hi", "ref", "/tmp", "Read", "system")
    assert direct[:3] == ["claude", "-p", "hi"]
    assert "--resume" in direct
    assert "ref" in direct
    shell = CliExecutor("claude", 30, "shell", "/bin/bash").build_command(
        "hi", None, "/tmp", None, None
    )
    assert shell[0] == "/bin/bash"
    assert shell[1] == "-c"
    assert shell[2].endswith("< /dev/null")


def test_repo_scan_validation_and_initial_message() -> None:
    """仓库扫描要求 purpose，并且不把首条输入落成用户消息。"""
    assert validate_result("REPO_SCAN", '{"purpose":"api","summaryMd":"md"}') is None
    assert validate_result("REPO_SCAN", '{"summaryMd":"md"}') == "missing or blank purpose field"
    assert validate_result("MEMORY_IMPORT", '{"items":[]}') == "items array is empty"
    scan = CreateSessionRequest(scene="REPO_SCAN", input="hello")
    assert should_persist_initial_user_message(scan) is False
    other = CreateSessionRequest(scene="CLARIFICATION", input="hello")
    assert should_persist_initial_user_message(other) is True
