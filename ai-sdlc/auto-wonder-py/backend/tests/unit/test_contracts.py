"""契约层：错误码、JWT、口令、AES-GCM、白名单、模型与端点清单。"""

import base64
import os
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import autowonder.model_imports  # noqa: F401
from autowonder.api.whitelist import is_login_only_request, is_whitelisted
from autowonder.config import get_settings
from autowonder.core.errors import ErrorCode
from autowonder.db.base import Base
from autowonder.db.tenant import TENANT_MODELS, TENANT_TABLES
from autowonder.jobs.catalog import SCHEDULED_JOBS
from autowonder.main import create_app
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.security.jwt import TokenPayload, parse_access, sign_access
from autowonder.security.password import encode, matches

BACKEND = Path(__file__).resolve().parents[2]


def test_error_codes_are_unique_and_include_success() -> None:
    codes = [item.code for item in ErrorCode]
    assert len(codes) == len(set(codes))
    assert ErrorCode.SUCCESS.code == "0"
    assert ErrorCode.WORKSPACE_NOT_MEMBER.code == "11001"
    assert ErrorCode.ORG_RESTORE_NAME_CONFLICT.code == "11007"


def test_error_codes_match_java_enum() -> None:
    java = os.environ.get("AUTOWONDER_JAVA_ROOT")
    if not java:
        pytest.skip("AUTOWONDER_JAVA_ROOT is not set")
    text = Path(java, "src/main/java/com/aliyun/autowonder/common/error/ErrorCode.java").read_text(
        encoding="utf-8"
    )
    pairs = re.findall(r'([A-Z0-9_]+)\("([^"]+)",\s*"([^"]*)"', text)
    java_codes = {name: code for name, code, _message in pairs}
    python_codes = {item.name: item.code for item in ErrorCode}
    assert python_codes == java_codes


def test_models_cover_schema_and_tenant_tables() -> None:
    tables = set(Base.metadata.tables)
    assert len(tables) == 84
    assert len(TENANT_TABLES) == 38
    registered = {model.__tablename__ for model in TENANT_MODELS}
    assert registered == set(TENANT_TABLES)
    for model in TENANT_MODELS:
        assert hasattr(model, "tenant_id")


def test_scheduled_job_catalog_has_eighteen_tasks() -> None:
    assert len(SCHEDULED_JOBS) == 18
    assert len({job.name for job in SCHEDULED_JOBS}) == 18


def test_bcrypt_uses_2a_and_verifies_spring_hash() -> None:
    hashed = encode("secret-pass")
    assert hashed.startswith("$2a$")
    assert matches("secret-pass", hashed)
    assert matches("secret-pass", "DEACTIVATED") is False


def test_jwt_round_trip_omits_empty_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOWONDER_JWT_SECRET", "autowonder-dev-jwt-secret-32-bytes")
    get_settings.cache_clear()
    token = sign_access(TokenPayload(user_id=42, workspace_id=None, jti="jti-1"))
    parsed = parse_access(token)
    assert parsed.user_id == 42
    assert parsed.workspace_id is None
    assert parsed.jti == "jti-1"
    scoped = sign_access(TokenPayload(user_id=7, workspace_id=9, jti="jti-2"))
    assert parse_access(scoped).workspace_id == 9


def test_aes_gcm_round_trip_and_rejects_tampering() -> None:
    key = base64.b64encode(b"k" * 32).decode()
    crypto = AesGcmSecretCrypto(key, nonce_source=lambda size: b"n" * size)
    token = crypto.encrypt("executor-token")
    assert token.startswith("enc:v1:")
    assert crypto.decrypt(token) == "executor-token"
    assert crypto.mask("abcdef") == "ab****ef"
    assert crypto.mask("ab") == "****"
    with pytest.raises(ValueError):
        crypto.decrypt(token[:-2] + "aa")


def test_auth_whitelist_matches_filter_rules() -> None:
    assert is_whitelisted("POST", "/api/auth/login")
    assert is_whitelisted("GET", "/api/hello")
    assert is_whitelisted("GET", "/api/platform/branding/public")
    assert is_whitelisted("POST", "/api/cli/workitems/15/requirement-documents")
    assert is_whitelisted("GET", "/api/cli/workitems/15/requirement-documents/index")
    assert is_whitelisted("GET", "/api/cli/workitems/15/requirement-documents/9/content")
    assert is_whitelisted("POST", "/api/cli/scheduled-tasks/15/documents")
    assert not is_whitelisted("GET", "/api/cli/workitems/15/requirement-documents")
    assert not is_whitelisted("POST", "/api/cli/workitems/15/requirement-documents/index")
    assert not is_whitelisted("GET", "/api/workitems")
    assert is_login_only_request("GET", "/api/workspaces/mine")
    assert is_login_only_request("POST", "/api/users/me/deactivation/revoke")
    assert is_login_only_request("GET", "/api/users/me/im-identities")
    assert is_login_only_request("PUT", "/api/platform/im-channels/dingtalk")
    assert not is_login_only_request("GET", "/api/workitems")


def test_status_taobao_follows_the_java_marker(tmp_path, monkeypatch) -> None:
    from autowonder.api import meta

    marker = tmp_path / "status.taobao"
    monkeypatch.setattr(meta, "STATUS_MARKER", marker)
    client = TestClient(create_app())
    missing = client.get("/status.taobao")
    assert missing.status_code == 404
    assert missing.text == (
        "HealthCheckController can not found META-INF/resources/status.taobao, "
        "please check app status.; server maybe in rebooting..."
    )
    marker.write_text("ok", encoding="utf-8")
    ready = client.get("/status.taobao")
    assert ready.status_code == 200
    assert ready.text == "success"


def test_missing_json_body_uses_the_java_param_envelope() -> None:
    client = TestClient(create_app())
    missing = client.post("/api/auth/login")
    assert missing.status_code == 400
    assert missing.json()["code"] == "10001"
    assert missing.json()["message"] == "参数不合法"
    assert missing.json()["data"] is None
    broken = client.post(
        "/api/auth/login",
        content=b"{",
        headers={"content-type": "application/json"},
    )
    assert broken.status_code == 400
    assert broken.json()["code"] == "10001"
    form = client.post("/api/daemon/dispatches/1/checkpoint")
    assert form.status_code == 200
    assert form.json()["code"] == "10000"
    assert form.json()["message"] == "系统内部错误"
    usage = client.post("/api/daemon/tasks/1/usage")
    assert usage.status_code == 400
    assert usage.json()["code"] == "10001"


def test_argument_binding_precedes_workspace_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """登录但没有工作空间时，缺参先于 11001，未映射的 Aone 控制器是 404。"""
    monkeypatch.setenv("AUTOWONDER_JWT_SECRET", "autowonder-dev-jwt-secret-32-bytes")
    get_settings.cache_clear()
    token = sign_access(TokenPayload(user_id=42, workspace_id=None, jti="binding-order"))
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app()) as client:
        _assert_binding_order(client, headers)


def _assert_binding_order(client: TestClient, headers: dict[str, str]) -> None:
    missing_body = client.post("/api/agents", headers=headers)
    assert missing_body.status_code == 400
    assert missing_body.json()["code"] == "10001"
    missing_query = client.get("/api/debug-logs", headers=headers)
    assert missing_query.status_code == 200
    assert missing_query.json()["code"] == "10000"
    listed = client.get("/api/agents", headers=headers)
    assert listed.status_code == 403
    assert listed.json()["code"] == "11001"
    setting = client.put("/api/users/me/settings/theme", headers=headers)
    assert setting.status_code == 400
    assert setting.json()["code"] == "10001"
    missing_controller = client.get("/api/integrations/aone/bindings", headers=headers)
    assert missing_controller.status_code == 404
    body = missing_controller.json()
    assert body["status"] == 404
    assert body["error"] == "Not Found"
    assert body["path"] == "/api/integrations/aone/bindings"
    assert body["timestamp"].endswith("+00:00")


def test_hello_envelope() -> None:
    client = TestClient(create_app())
    response = client.get("/api/hello")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["code"] == "0"
    assert body["data"] == "Hello from AutoWonder!"
    assert body["message"] == ""
    assert "request_id" in body


def test_missing_token_is_unauthorized_envelope() -> None:
    client = TestClient(create_app())
    response = client.get("/api/workitems")
    assert response.status_code == 401
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "10401"
    assert "traceId" not in body


def test_endpoint_catalog_contains_auth_login() -> None:
    catalog = yaml.safe_load(
        (BACKEND / "verify" / "parity" / "cases" / "endpoints.yaml").read_text(encoding="utf-8")
    )
    assert {"method": "POST", "path": "/api/auth/login", "controller": "AuthController"} in catalog
    assert {
        "method": "GET",
        "path": "/checkpreload.htm",
        "controller": "HealthCheckController",
    } in catalog
    assert {
        "method": "GET",
        "path": "/status.taobao",
        "controller": "HealthCheckController",
    } in catalog
    assert len(catalog) == 375
