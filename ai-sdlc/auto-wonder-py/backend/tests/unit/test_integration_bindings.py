"""集成绑定、回执和外部工单导入的路径与纯函数行为。"""

import base64

import pytest
from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode, IllegalArgumentError
from autowonder.integrations.aone_codec import (
    AoneDisabledError,
    require_enabled,
    sign_aone,
    to_url_encoded_query,
)
from autowonder.integrations.dingtalk_bindings import (
    _status_or_default,
    _stream_env_or_default,
)
from autowonder.integrations.dingtalk_bindings import (
    router as dingtalk_router,
)
from autowonder.integrations.extra_router import (
    aone_router,
    import_router,
    receipt_router,
    sync_router,
)
from autowonder.integrations.feishu_bindings import callback_router
from autowonder.integrations.feishu_bindings import router as feishu_router
from autowonder.integrations.feishu_security import FeishuSecrets, SecurityError, verify_callback
from autowonder.integrations.receipts_sanitize import sanitize_text
from autowonder.integrations.workitem_import import normalize_work_type
from autowonder.main import create_app

_PATHS = {
    "/api/integrations/aone/bindings": {"get", "post"},
    "/api/integrations/aone/bindings/test": {"post"},
    "/api/integrations/aone/bindings/{id}/sync-now": {"post"},
    "/api/integrations/aone/outbox/dispatch-now": {"post"},
    "/api/integrations/aone/projects/search": {"post"},
    "/api/integrations/aone/projects/{projectId}/members": {"post"},
    "/api/integrations/dingtalk/bindings": {"get", "post"},
    "/api/integrations/dingtalk/bindings/{id}": {"get", "put", "delete"},
    "/api/integrations/feishu/bindings": {"get", "post"},
    "/api/integrations/feishu/bindings/{id}": {"put", "delete"},
    "/api/integrations/feishu/callback": {"post"},
    "/api/integrations/receipts/{id}/confirm-succeeded": {"post"},
    "/api/integrations/receipts/{id}/retry": {"post"},
    "/api/v1/external/workitems/import": {"post"},
    "/api/v1/external/workitems/import-records": {"get"},
    "/api/workitems/{id}/external-sync": {"post"},
}

_AUTH_PATHS = [
    ("get", "/api/integrations/aone/bindings"),
    ("get", "/api/integrations/dingtalk/bindings"),
    ("get", "/api/integrations/feishu/bindings"),
    ("post", "/api/integrations/receipts/1/retry"),
    ("get", "/api/v1/external/workitems/import-records"),
    ("post", "/api/workitems/1/external-sync"),
]


def _client() -> TestClient:
    app = create_app()
    app.include_router(aone_router)
    app.include_router(dingtalk_router)
    app.include_router(feishu_router)
    app.include_router(callback_router)
    app.include_router(receipt_router)
    app.include_router(import_router)
    app.include_router(sync_router)
    return TestClient(app)


def test_integration_routes_exist() -> None:
    """控制器路径和方法都挂在应用上。"""
    client = _client()
    paths = client.app.openapi()["paths"]
    for path, methods in _PATHS.items():
        assert methods <= set(paths[path])


def test_protected_integration_routes_require_login() -> None:
    """未带令牌时返回 401，业务码 10401。"""
    client = _client()
    for method, path in _AUTH_PATHS:
        response = client.request(method, path)
        assert response.status_code == 401
        assert response.json()["code"] == "10401"


def test_capabilities_route_stays_public() -> None:
    """追加路由后，原有能力查询仍公开且默认关闭 Aone。"""
    client = _client()
    response = client.get("/api/integrations/capabilities")
    assert response.status_code == 200
    assert response.json()["data"] == {"aoneEnabled": False}


def test_aone_disabled_still_rejects_remote_calls() -> None:
    """默认关闭时走 requireEnabled，而不是直接返回成功。"""
    with pytest.raises(AoneDisabledError, match="Aone integration is disabled"):
        require_enabled()


def test_aone_signature_and_query() -> None:
    """签名去掉填充，表单跳过空值并把空格写成加号。"""
    secret = base64.b64encode(b"0123456789abcdef").decode()
    signed = sign_aone("auto-wonder", secret, 1_700_000_000_000)
    assert signed == sign_aone("auto-wonder", secret, 1_700_000_000_000)
    assert "=" not in signed
    assert "+" not in signed
    assert "/" not in signed
    assert to_url_encoded_query({"a": None, "b": [], "c": "x y"}) == "c=x+y"


def test_receipt_reason_redacts_inline_secrets() -> None:
    """原因文本里的 token 和 secret 会被抹掉。"""
    assert sanitize_text("token=abc secret:xyz") == "token=[REDACTED] secret:[REDACTED]"


def test_import_work_type_mapping() -> None:
    """外部类型名收成三种工单类型，其余拒绝。"""
    assert normalize_work_type("story") == "REQ"
    assert normalize_work_type("defect") == "BUG"
    assert normalize_work_type("task") == "TASK"
    with pytest.raises(BizError) as caught:
        normalize_work_type("epic")
    assert caught.value.error_code == ErrorCode.WORK_TYPE_INVALID


def test_dingtalk_status_and_stream_env() -> None:
    """空白用默认值，不支持的环境和状态直接拒绝。"""
    assert _status_or_default(None) == "ENABLED"
    assert _status_or_default("disabled") == "DISABLED"
    assert _stream_env_or_default(" online ") == "ONLINE"
    with pytest.raises(IllegalArgumentError):
        _stream_env_or_default("PRE")


def test_feishu_callback_rejects_bad_token() -> None:
    """令牌不一致时验签失败。"""
    with pytest.raises(SecurityError):
        verify_callback(
            '{"type":"url_verification","token":"nope","challenge":"c"}',
            FeishuSecrets("secret", "expected", None),
            None,
            None,
            None,
            1_700_000_000,
        )
