"""平台 IM 通道和个人身份。"""

import json

import pytest
from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.im.channels import UpdateChannelRequest, list_channels, update_channel
from autowonder.im.identities import capability, list_identities, send_test, update_identity
from autowonder.im.models import PlatformImChannelConfig, PlatformImSelection, UserImIdentity
from autowonder.im.providers import ImDeliveryError, ImProviderRegistry, ImSendCommand
from autowonder.im.router import save_channel
from autowonder.main import create_app
from autowonder.platform.models import PlatformBrandingConfig
from autowonder.users.models import User
from tests.unit.test_workitems import MemorySession


class _Cipher:
    def __init__(self) -> None:
        self.encrypted: list[str] = []

    def encrypt(self, plaintext: str) -> str:
        self.encrypted.append(plaintext)
        return "kms://" + plaintext

    def decrypt(self, ciphertext: str) -> str:
        return ciphertext.removeprefix("kms://")


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.command: ImSendCommand | None = None
        self.error: Exception | None = None

    def provider(self) -> str:
        return self.name

    async def send(self, command: ImSendCommand) -> None:
        if self.error is not None:
            raise self.error
        self.command = command


def _request(
    enabled: bool,
    app_key: str | None,
    app_secret: str | None,
    robot_code: str | None,
    base_url: str | None = " https://api.dingtalk.com ",
) -> UpdateChannelRequest:
    return UpdateChannelRequest(
        enabled=enabled,
        app_key=app_key,
        app_secret=app_secret,
        robot_code=robot_code,
        base_url=base_url,
    )


def test_im_routes_require_login_without_workspace_access() -> None:
    """个人身份和通道列表都要登录，不挂工作空间访问级别。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "get" in paths["/api/users/me/im-identities"]
    assert "put" in paths["/api/users/me/im-identities/dingtalk"]
    assert "post" in paths["/api/users/me/im-identities/dingtalk/test"]
    assert "put" in paths["/api/users/me/im-identities/feishu"]
    assert "get" in paths["/api/platform/im-channels"]
    assert "put" in paths["/api/platform/im-channels/dingtalk"]
    assert "put" in paths["/api/platform/im-channels/feishu"]
    listed = client.get("/api/users/me/im-identities")
    assert listed.status_code == 401
    assert listed.json()["code"] == "10401"
    channels = client.get("/api/platform/im-channels")
    assert channels.status_code == 401


async def test_channel_update_encrypts_secret_and_hides_it() -> None:
    """密钥只以密文入库，响应里没有明文和密文引用。"""
    session = MemorySession()
    cipher = _Cipher()
    view = await update_channel(
        session,
        100,
        "dingtalk",
        _request(True, " app-key ", "app-secret", " robot-code "),
        cipher,
    )
    assert cipher.encrypted == ["app-secret"]
    stored = next(row for row in session.rows if isinstance(row, PlatformImChannelConfig))
    assert stored.provider == "DINGTALK"
    assert stored.app_key == "app-key"
    assert stored.credential_ref == "kms://app-secret"
    assert view.secret_configured is True
    assert view.ready is True
    assert view.selected is True
    payload = json.dumps(view.model_dump(by_alias=True))
    assert "appSecret" not in payload
    assert "credentialRef" not in payload
    assert "kms://encrypted" not in payload
    assert "app-secret" not in payload


async def test_blank_secret_keeps_the_existing_credential() -> None:
    """空白密钥不重新加密，原来的密文继续留在行上。"""
    session = MemorySession()
    session.add(
        PlatformImChannelConfig(
            provider="DINGTALK",
            enabled=1,
            app_key="old-key",
            credential_ref="kms://existing",
            robot_code="old-robot",
            base_url="https://api.dingtalk.com",
        )
    )
    await session.flush()
    cipher = _Cipher()
    view = await update_channel(
        session,
        100,
        "DINGTALK",
        _request(True, "new-key", "  ", "new-robot"),
        cipher,
    )
    assert cipher.encrypted == []
    stored = next(row for row in session.rows if isinstance(row, PlatformImChannelConfig))
    assert stored.credential_ref == "kms://existing"
    assert stored.app_key == "new-key"
    assert view.secret_configured is True


async def test_enabling_without_a_secret_is_rejected() -> None:
    """第一次启用但没有密钥时不落库。"""
    session = MemorySession()
    with pytest.raises(BizError) as error:
        await update_channel(
            session,
            100,
            "DINGTALK",
            _request(True, "app-key", None, "robot-code"),
            _Cipher(),
        )
    assert error.value.code == "28001"
    assert not any(isinstance(row, PlatformImChannelConfig) for row in session.rows)


async def test_channel_urls_and_lengths() -> None:
    """超长字段和非 HTTPS 地址在写入前拒绝，合法反代地址去掉结尾斜杠。"""
    session = MemorySession()
    cipher = _Cipher()
    rejected = [
        _request(False, "a" * 129, None, None),
        _request(False, None, "s" * 1025, None),
        _request(False, None, None, "r" * 129),
        _request(False, None, None, None, "http://api.dingtalk.com"),
        _request(False, None, None, None, "https://user@api.dingtalk.com/proxy"),
        _request(False, None, None, None, "https://api.dingtalk.com/proxy?tenant=one"),
        _request(False, None, None, None, "https://api.dingtalk.com/proxy#fragment"),
    ]
    for request in rejected:
        with pytest.raises(BizError) as error:
            await update_channel(session, 100, "DINGTALK", request, cipher)
        assert error.value.code == ErrorCode.PARAM_INVALID.code
    assert not any(isinstance(row, PlatformImChannelConfig) for row in session.rows)
    await update_channel(
        session,
        100,
        "DINGTALK",
        _request(False, None, None, None, " https://gateway.example.com/dingtalk/proxy/// "),
        cipher,
    )
    stored = next(row for row in session.rows if isinstance(row, PlatformImChannelConfig))
    assert stored.base_url == "https://gateway.example.com/dingtalk/proxy"


async def test_feishu_can_be_ready_without_a_robot_and_becomes_selected() -> None:
    """飞书不要求机器人编码。保存后其他渠道关闭，选择切到飞书。"""
    session = MemorySession()
    session.add(
        PlatformImChannelConfig(
            provider="DINGTALK",
            enabled=1,
            app_key="ding",
            credential_ref="kms://ding",
            robot_code="robot",
            base_url="https://api.dingtalk.com",
        )
    )
    await session.flush()
    view = await update_channel(
        session,
        100,
        "feishu",
        _request(True, "feishu-app", "feishu-secret", None, "https://open.feishu.cn"),
        _Cipher(),
    )
    assert view.provider == "FEISHU"
    assert view.ready is True
    assert view.selected is True
    dingtalk = next(
        row
        for row in session.rows
        if isinstance(row, PlatformImChannelConfig) and row.provider == "DINGTALK"
    )
    assert dingtalk.enabled == 0
    selection = next(row for row in session.rows if isinstance(row, PlatformImSelection))
    assert selection.provider == "FEISHU"
    listed = await list_channels(session, 100)
    assert [item.provider for item in listed] == ["DINGTALK", "FEISHU"]
    assert [item.selected for item in listed] == [False, True]


async def test_non_admin_cannot_save_a_channel() -> None:
    """通道写入先检查平台管理员。"""
    session = MemorySession()
    session.add(User(id=7, username="member", password_hash="x", is_admin=0))
    await session.flush()
    with pytest.raises(BizError) as error:
        await save_channel(session, 7, "DINGTALK", _request(False, None, None, None))
    assert error.value.code == "10403"
    assert error.value.args[0] == "仅平台管理员可以配置协作通知"
    assert not any(isinstance(row, PlatformImChannelConfig) for row in session.rows)


async def test_identity_upsert_trim_and_blank_delete() -> None:
    """工号去掉两端空白；空白工号软删，且不能改未选中的渠道。"""
    session = MemorySession()
    saved = await update_identity(session, 200, "dingtalk", " staff-001 ")
    assert saved.provider == "DINGTALK"
    assert saved.external_user_id == "staff-001"
    assert saved.configured is True
    assert saved.platform_ready is False
    assert saved.test_available is False
    row = next(item for item in session.rows if isinstance(item, UserImIdentity))
    assert row.user_id == 200
    assert not hasattr(row, "tenant_id")
    cleared = await update_identity(session, 200, "DINGTALK", "  ")
    assert cleared.configured is False
    assert cleared.external_user_id is None
    assert row.is_deleted == 1
    with pytest.raises(BizError) as wrong:
        await update_identity(session, 200, "FEISHU", "ou_1")
    assert wrong.value.code == "10001"
    assert wrong.value.args[0] == "请使用平台当前选择的 IM 渠道"
    with pytest.raises(BizError) as long_id:
        await update_identity(session, 200, "DINGTALK", "u" * 257)
    assert long_id.value.code == "10001"


async def test_capability_and_test_message() -> None:
    """通道就绪且工号存在才能测试。失败时响应不含工号。"""
    session = MemorySession()
    await update_channel(
        session,
        100,
        "DINGTALK",
        _request(True, "app-key", "app-secret", "robot"),
        _Cipher(),
    )
    await update_identity(session, 200, "dingtalk", "staff-001")
    ready = await capability(session, 200, "dingtalk")
    assert ready.configured is True
    assert ready.platform_ready is True
    assert ready.test_available is True
    missing = await capability(session, 201, "DINGTALK")
    assert missing.configured is False
    assert missing.platform_ready is True
    assert missing.test_available is False
    session.add(
        PlatformBrandingConfig(
            id=1,
            platform_name="Acme Platform",
            theme_key="teal",
            primary_color="#008080",
        )
    )
    await session.flush()
    provider = _Provider("DINGTALK")
    await send_test(session, 200, "dingtalk", ImProviderRegistry([provider]))
    assert provider.command is not None
    assert provider.command.provider == "DINGTALK"
    assert provider.command.external_user_id == "staff-001"
    assert provider.command.title == "Acme Platform 协作通知测试成功"
    assert "Acme Platform 协作通知测试成功" in provider.command.markdown
    listed = await list_identities(session, 200)
    assert listed[0].external_user_id == "staff-001"


async def test_send_rejects_missing_identity_and_hides_provider_secrets() -> None:
    """没工号是 28002，通道没就绪是 28001，供应商失败是 28003。"""
    session = MemorySession()
    provider = _Provider("DINGTALK")
    registry = ImProviderRegistry([provider])
    with pytest.raises(BizError) as missing:
        await send_test(session, 200, "dingtalk", registry)
    assert missing.value.code == "28002"
    await update_identity(session, 200, "DINGTALK", "staff-secret-id")
    with pytest.raises(BizError) as unavailable:
        await send_test(session, 200, "dingtalk", registry)
    assert unavailable.value.code == "28001"
    assert provider.command is None
    await update_channel(
        session,
        100,
        "DINGTALK",
        _request(True, "app-key", "app-secret", "robot"),
        _Cipher(),
    )
    provider.error = ImDeliveryError(
        "DINGTALK",
        False,
        "authFailed",
        "req-1",
    )
    provider.error.__cause__ = RuntimeError("secret-value staff-secret-id")
    with pytest.raises(BizError) as failed:
        await send_test(session, 200, "dingtalk", registry)
    assert failed.value.code == "28003"
    assert "secret-value" not in str(failed.value)
    assert "staff-secret-id" not in str(failed.value)
