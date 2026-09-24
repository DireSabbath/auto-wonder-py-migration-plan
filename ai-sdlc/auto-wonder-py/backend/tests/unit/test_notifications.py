"""通知分页、偏好开关和路径。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.im.providers import require_selected_provider
from autowonder.main import create_app
from autowonder.notifications.models import NotifyPref
from autowonder.notifications.schemas import UpdatePrefRequest
from autowonder.notifications.service import (
    MISSING_NOTIFICATION,
    display_channels,
    java_parse_boolean,
    page_window,
    prefs_unchanged,
    should_deliver,
)


def test_notification_page_prefs_and_delivery() -> None:
    """分页上限 100；未选中的 IM 不展示；没有偏好时渠道都投递。"""
    assert page_window(0, 500) == (0, 100)
    assert page_window(2, 10) == (10, 10)
    assert display_channels(1, 1, 1, "DINGTALK") == (True, True, False)
    assert display_channels(0, 1, 1, "FEISHU") == (False, False, True)
    assert display_channels(None, None, None, "DINGTALK") == (False, False, False)
    assert prefs_unchanged(None) is True
    assert prefs_unchanged([]) is True
    body = UpdatePrefRequest.model_validate(
        {"items": [{"type": "MENTION", "inApp": True, "dingtalk": False}]}
    )
    assert body.items is not None
    assert prefs_unchanged(body.items) is False
    assert body.items[0].in_app is True
    assert body.items[0].feishu is False
    assert should_deliver("inApp", None) is True
    assert should_deliver("dingtalk", None) is True
    closed = NotifyPref(tenant_id=1, user_id=2, type="MENTION", in_app=0, dingtalk=1, feishu=0)
    assert should_deliver("inApp", closed) is False
    assert should_deliver("dingtalk", closed) is True
    assert should_deliver("feishu", closed) is False
    assert java_parse_boolean(None) is False
    assert java_parse_boolean("true") is True
    assert java_parse_boolean('"true"') is True
    assert java_parse_boolean("false") is False
    assert MISSING_NOTIFICATION == "通知不存在"
    try:
        require_selected_provider("DINGTALK", "FEISHU")
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
        assert str(error) == "请使用平台当前选择的 IM 渠道"
    else:
        raise AssertionError("expected unselected IM provider")


def test_notification_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/notifications" in paths
    assert "/api/notifications/unread-count" in paths
    assert "/api/notifications/read-all" in paths
    assert "/api/notifications/prefs" in paths
    assert "/api/notifications/{id}" in paths
    assert "/api/notifications/{id}/read" in paths
    response = client.get("/api/notifications")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
