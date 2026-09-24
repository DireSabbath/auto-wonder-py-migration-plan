"""用户偏好校验、注销状态和 ``/api/users/me`` 路由。这些检查不访问数据库。"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app
from autowonder.users.deactivation import (
    ACTIVE_ASSIGNEE_SQL,
    COOLING_OFF_DAYS,
    account_is_disabled,
    active_workitem_block_message,
    confirm_username_matches,
    cooling_off_deadline,
    deactivation_expired,
    deactivation_view,
    in_cooling_off,
    sole_admin_statement,
)
from autowonder.users.preferences import (
    MAX_KEY_LENGTH,
    MAX_VALUE_JSON_LENGTH,
    java_utf16_length,
    json_text,
    normalize_value_json,
    to_setting_view,
    validate_setting_key,
)

_NOW = datetime(2026, 9, 23, 12, 0, 0)


def test_setting_key_and_json_follow_java_limits() -> None:
    """空键、超长键和非法 JSON 拒绝；达到列宽上限的键和 JSON 文本可以留下。"""
    assert java_utf16_length("👍") == 2
    assert validate_setting_key("k" * MAX_KEY_LENGTH) == "k" * MAX_KEY_LENGTH
    try:
        validate_setting_key(None)
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected missing setting key")
    try:
        validate_setting_key("  ")
    except BizError as error:
        assert error.code == "10001"
    else:
        raise AssertionError("expected blank setting key")
    try:
        validate_setting_key("k" * 127 + "👍")
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected utf-16 key overflow")
    echoed, parsed = normalize_value_json('"shift-enter"')
    assert echoed == '"shift-enter"'
    assert parsed == "shift-enter"
    echoed, parsed = normalize_value_json('{"rows":6,"pinned":true}')
    assert echoed == '{"rows":6,"pinned":true}'
    assert parsed == {"rows": 6, "pinned": True}
    assert normalize_value_json(None) == (None, None)
    at_limit = '"' + "x" * (MAX_VALUE_JSON_LENGTH - 2) + '"'
    assert java_utf16_length(at_limit) == MAX_VALUE_JSON_LENGTH
    assert normalize_value_json(at_limit)[0] == at_limit
    for value in ("shift-enter", "   ", '"' + "x" * MAX_VALUE_JSON_LENGTH + '"'):
        try:
            normalize_value_json(value)
        except BizError as error:
            assert error.error_code == ErrorCode.PARAM_INVALID
        else:
            raise AssertionError("expected invalid setting value")
    assert json_text("shift-enter") == '"shift-enter"'
    assert json_text(6) == "6"
    assert json_text({"rows": 6, "pinned": True}) == '{"rows":6,"pinned":true}'
    assert to_setting_view("clarification_send_mode", None).value_json is None


def test_deactivation_status_matches_java_flags() -> None:
    """冷静期内返回时间；过期且已禁用，或已经撤销，都标成 revoked。"""
    assert cooling_off_deadline(_NOW) == _NOW + timedelta(days=COOLING_OFF_DAYS)
    assert confirm_username_matches("testuser", "testuser") is True
    assert confirm_username_matches("wronguser", "testuser") is False
    assert confirm_username_matches(None, "testuser") is False
    assert account_is_disabled(1) is True
    assert account_is_disabled(0) is False
    assert account_is_disabled(None) is False
    idle = deactivation_view(None, None, None, 0, _NOW)
    assert idle.pending is False
    assert idle.deactivated_at is None
    assert idle.revoked is False
    expires = _NOW + timedelta(days=5)
    pending = deactivation_view(_NOW, expires, None, 0, _NOW)
    assert pending.pending is True
    assert pending.deactivated_at == _NOW
    assert pending.cooling_off_expires_at == expires
    assert pending.revoked is False
    boundary = deactivation_view(_NOW, _NOW, None, 0, _NOW)
    assert boundary.pending is False
    assert in_cooling_off(_NOW, _NOW, None, _NOW) is False
    expired = deactivation_view(_NOW, _NOW - timedelta(seconds=1), None, 1, _NOW)
    assert expired.pending is False
    assert expired.deactivated_at is None
    assert expired.revoked is True
    assert deactivation_expired(_NOW, _NOW - timedelta(seconds=1), None, 0, _NOW) is False
    revoked = deactivation_view(_NOW, expires, _NOW, 0, _NOW)
    assert revoked.pending is False
    assert revoked.revoked is True
    assert active_workitem_block_message(3) == "存在 3 个未完结的工单，请先处理后再申请注销"
    sql = str(ACTIVE_ASSIGNEE_SQL)
    assert "tenant_id" not in sql
    assert "NOT IN ('DONE', 'CANCELED')" in sql
    compiled = str(
        sole_admin_statement(7).compile(
            dialect=mysql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "ADMIN" in compiled
    assert "NOT (EXISTS" in compiled


def test_user_account_routes_require_login() -> None:
    """账号、注销和偏好路径与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/users/me/password" in paths
    assert "put" in paths["/api/users/me/password"]
    assert "/api/users/me/deactivation" in paths
    assert "get" in paths["/api/users/me/deactivation"]
    assert "post" in paths["/api/users/me/deactivation"]
    assert "/api/users/me/deactivation/revoke" in paths
    assert "/api/users/me/settings" in paths
    assert "/api/users/me/settings/{key}" in paths
    assert "delete" in paths["/api/users/me/settings/{key}"]
    response = client.get("/api/users/me/settings")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
    response = client.put("/api/users/me/password", json={})
    assert response.status_code == 401
    response = client.post("/api/users/me/deactivation/revoke")
    assert response.status_code == 401
