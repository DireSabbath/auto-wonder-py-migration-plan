"""设置分组、通知渠道键和密文落库。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.im.providers import normalize_provider, resolve_selected_provider
from autowonder.main import create_app
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.settings.schemas import SettingItem
from autowonder.settings.service import (
    allowed_notify_key,
    display_value,
    encode_item,
    json_text,
    require_group,
)

_MASTER_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def test_setting_group_notify_key_and_secret_storage() -> None:
    """非法分组拒绝；未选中的 IM 键不可写；密文不进 value_json。"""
    assert require_group("SYSTEM") == "SYSTEM"
    try:
        require_group("INVALID")
    except BizError as error:
        assert error.error_code == ErrorCode.SETTING_GROUP_INVALID
    else:
        raise AssertionError("expected invalid setting group")
    assert resolve_selected_provider(None) == "DINGTALK"
    assert normalize_provider(" feishu ") == "FEISHU"
    try:
        normalize_provider(" ")
    except BizError as error:
        assert str(error) == "IM provider 不能为空"
    else:
        raise AssertionError("expected blank provider")
    assert allowed_notify_key("dingtalk_enabled", "FEISHU") is False
    assert allowed_notify_key("feishu_enabled", "FEISHU") is True
    assert allowed_notify_key("email", "FEISHU") is True
    assert allowed_notify_key(None, "FEISHU") is False
    crypto = AesGcmSecretCrypto(_MASTER_KEY)
    secret = SettingItem(key="api_key", value_json="sk-secret-value", secret=True)
    value, is_secret, credential = encode_item(secret, crypto)
    assert value is None
    assert is_secret == 1
    assert credential is not None
    assert crypto.decrypt(credential) == "sk-secret-value"
    plain = SettingItem(key="timeout", value_json='"30"')
    assert plain.secret is False
    stored, is_secret, credential = encode_item(plain, crypto)
    assert is_secret == 0
    assert credential is None
    assert json_text(stored) == '"30"'
    assert display_value(1, None, "ref:sk-ant-abc123xyz", crypto) == "re****yz"


def test_setting_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/settings/{group}" in paths
    response = client.get("/api/settings/AI")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
