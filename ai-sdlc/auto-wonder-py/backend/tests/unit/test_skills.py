"""技能安装规格、分页窗口和分类参数。这些检查不访问数据库。"""

import json

import pytest
from fastapi.testclient import TestClient

from autowonder.config import Settings
from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.skills.install_spec import (
    display_install_spec,
    is_mcp_type,
    normalize_install_spec,
    reject_packaged_capability,
)
from autowonder.skills.schemas import category_id_from_json, skill_ids_from_json
from autowonder.skills.service import page_window

_MASTER_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def test_page_window_and_packaged_types() -> None:
    """页码和页大小按 Java 收口；插件和 Hook 不能走手工入口。"""
    assert page_window(0, 0) == (1, 1)
    assert page_window(2, 20) == (2, 20)
    assert page_window(1, 101) == (1, 100)
    reject_packaged_capability("SKILL")
    reject_packaged_capability(" MCP ")
    try:
        reject_packaged_capability(" Plugin ")
    except BizError as error:
        assert str(error) == "插件和 Runtime Hook 必须通过安装包入口配置"
    else:
        raise AssertionError("expected plugin type to be rejected")
    try:
        reject_packaged_capability("hook")
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
    else:
        raise AssertionError("expected hook type to be rejected")


def test_install_spec_keeps_non_mcp_text() -> None:
    """空白规格存空字符串；非法 JSON 保留原文；MCP 比较不裁剪空白。"""
    assert is_mcp_type("McP") is True
    assert is_mcp_type(" MCP ") is False
    assert normalize_install_spec("SKILL", "   ", None) == ""
    assert normalize_install_spec("SKILL", "  not-json  ", None) == "  not-json  "
    assert normalize_install_spec("SKILL", ' {"a": 1} ', None) == {"a": 1}
    assert normalize_install_spec(" MCP ", "not-json", None) == "not-json"
    assert display_install_spec("") == ""
    assert display_install_spec(None) is None


def test_mcp_config_rules_and_secret_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP、stdio、超时和私密项按 Java 校验；响应不回传密文引用。"""
    monkeypatch.setattr(
        "autowonder.skills.install_spec.get_settings",
        lambda: Settings(AUTOWONDER_SECRET_MASTER_KEY=_MASTER_KEY),
    )
    try:
        normalize_install_spec("MCP", "[1]", None)
    except BizError as error:
        assert str(error) == "MCP 配置必须是 JSON 对象"
    else:
        raise AssertionError("expected MCP array to fail")
    try:
        normalize_install_spec("MCP", '{"url":"ftp://example.com"}', None)
    except BizError as error:
        assert str(error) == "MCP 地址必须是 HTTP/HTTPS URL"
    else:
        raise AssertionError("expected invalid MCP url")
    try:
        normalize_install_spec(
            "MCP",
            '{"transport":"stdio","command":"npx","headers":{"A":"1"}}',
            None,
        )
    except BizError as error:
        assert str(error) == "stdio MCP 不支持请求头或超时配置"
    else:
        raise AssertionError("expected stdio headers to fail")
    try:
        normalize_install_spec("MCP", '{"url":"https://example.com","timeoutSeconds":0}', None)
    except BizError as error:
        assert str(error) == "MCP 超时时间必须在 1 到 600 秒之间"
    else:
        raise AssertionError("expected timeout range")
    stored = normalize_install_spec(
        "MCP",
        '{"url":"https://example.com/mcp",'
        '"headers":{"Authorization":{"secret":true,"value":"token"}}}',
        None,
    )
    assert isinstance(stored, dict)
    headers = stored["headers"]
    assert isinstance(headers, dict)
    secret = headers["Authorization"]
    assert isinstance(secret, dict)
    assert secret["kind"] == "secretRef"
    assert AesGcmSecretCrypto(_MASTER_KEY).decrypt(str(secret["ref"])) == "token"
    assert stored["transport"] == "http"
    assert stored["timeoutSeconds"] == 60
    shown = display_install_spec(stored)
    assert shown is not None
    assert '"secret":true' in shown
    assert "enc:v1:" not in shown
    reused = normalize_install_spec(
        "mcp",
        '{"url":"https://example.com/mcp",'
        '"headers":{"authorization":{"secret":"true","value":" "}}}',
        stored,
    )
    assert isinstance(reused, dict)
    reused_headers = reused["headers"]
    assert isinstance(reused_headers, dict)
    reused_secret = reused_headers["authorization"]
    assert isinstance(reused_secret, dict)
    assert reused_secret["ref"] == secret["ref"]
    local = normalize_install_spec(
        "MCP",
        '{"transport":"stdio","command":" npx ","env":{"PATH":"/bin"}}',
        None,
    )
    assert isinstance(local, dict)
    assert local["env"] == {"PATH": "/bin"}


def test_mcp_header_names_and_missing_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """保留头和重复名称拒绝；没有主密钥时私密项不能落库。"""
    monkeypatch.setattr(
        "autowonder.skills.install_spec.get_settings",
        lambda: Settings(AUTOWONDER_SECRET_MASTER_KEY=_MASTER_KEY),
    )
    try:
        normalize_install_spec(
            "MCP",
            '{"url":"https://example.com","headers":{"Host":"example.com"}}',
            None,
        )
    except BizError as error:
        assert "名称不合法" in str(error)
    else:
        raise AssertionError("expected reserved header")
    try:
        normalize_install_spec(
            "MCP",
            '{"url":"https://example.com","headers":{"A":"1","a":"2"}}',
            None,
        )
    except BizError as error:
        assert "名称不能重复" in str(error)
    else:
        raise AssertionError("expected duplicate header")
    long_value = "a" * 4097
    try:
        normalize_install_spec(
            "MCP",
            json.dumps({"url": "https://example.com", "headers": {"X-Token": long_value}}),
            None,
        )
    except BizError as error:
        assert "值不合法" in str(error)
    else:
        raise AssertionError("expected long header value")
    monkeypatch.setattr(
        "autowonder.skills.install_spec.get_settings",
        lambda: Settings(AUTOWONDER_SECRET_MASTER_KEY=""),
    )
    try:
        normalize_install_spec(
            "MCP",
            '{"url":"https://example.com",'
            '"headers":{"Authorization":{"kind":"secretRef","value":"token"}}}',
            None,
        )
    except RuntimeError as error:
        assert str(error) == "密文存储未配置，无法保存私密 MCP 配置"
    else:
        raise AssertionError("expected missing master key")


def test_category_json_matches_java() -> None:
    """打标请求必须显式带上 categoryId；skillIds 只能是正整数。"""
    assert category_id_from_json({"categoryId": None}) is None
    assert category_id_from_json({"categoryId": 3}) == 3
    try:
        category_id_from_json({})
    except BizError as error:
        assert str(error) == "缺少 categoryId 参数"
    else:
        raise AssertionError("expected missing categoryId")
    try:
        category_id_from_json({"categoryId": 1.5})
    except BizError as error:
        assert str(error) == "categoryId 必须是正整数或 null"
    else:
        raise AssertionError("expected non-integral categoryId")
    assert skill_ids_from_json({"skillIds": [1, 2]}) == [1, 2]
    try:
        skill_ids_from_json({"skillIds": [1, True]})
    except BizError as error:
        assert str(error) == "skillIds 必须是数字数组"
    else:
        raise AssertionError("expected non-integral skill id")
    try:
        skill_ids_from_json({"categoryId": 1})
    except BizError as error:
        assert str(error) == "缺少 skillIds 参数"
    else:
        raise AssertionError("expected missing skillIds")


def test_skill_routes_match_java_and_require_login() -> None:
    """已迁移的技能路径与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/skills" in paths
    assert "/api/skills/{id}" in paths
    assert "/api/skills/{id}/category" in paths
    assert "/api/skills/category/batch" in paths
    assert "/api/skills/package" in paths
    assert "/api/skills/package/inspect" in paths
    assert "/api/skills/{id}/package" in paths
    assert "/api/skills/{id}/package/files" in paths
    assert "/api/skills/{id}/package/file" in paths
    assert "/api/skills/{id}/package/download" in paths
    assert "/api/skills/{id}/connection-test" not in paths
    response = client.get("/api/skills")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
