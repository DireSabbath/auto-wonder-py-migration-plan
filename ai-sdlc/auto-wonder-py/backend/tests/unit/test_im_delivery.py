"""钉钉出站发送和飞书协作通知。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

import httpx
import pytest

from autowonder.im.dingtalk import DingTalkImProvider
from autowonder.im.feishu import FeishuImProvider
from autowonder.im.models import PlatformImChannelConfig
from autowonder.im.providers import ImDeliveryError, ImSendCommand
from autowonder.im.router import _registry
from autowonder.integrations.dingtalk.errors import DingTalkHttpError, DingTalkTransportError
from autowonder.integrations.dingtalk.sender import (
    DEFAULT_BASE_URL,
    DingTalkOutboundSender,
    HttpxDingTalkExchange,
    network_failure,
)
from tests.unit.test_workitems import MemorySession


class _Http:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def post(self, url: str, json_body: str, headers: dict[str, str]) -> str:
        self.calls.append((url, json_body, dict(headers)))
        return self._responses.pop(0)


class _Configs:
    def __init__(self, row: PlatformImChannelConfig | None, secret: str | None) -> None:
        self.row = row
        self.secret = secret
        self.lookups: list[str] = []

    async def find_enabled(self, provider: str) -> PlatformImChannelConfig | None:
        self.lookups.append(provider)
        return self.row

    def decrypt_secret(self, row: PlatformImChannelConfig | None) -> str | None:
        return self.secret


class _Sender:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.error: BaseException | None = None

    def send_single_markdown(
        self,
        app_key: str,
        app_secret: str,
        base_url: str | None,
        robot_code: str,
        user_ids: list[str] | None,
        markdown: str,
        now_ms: int,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((app_key, app_secret, base_url, robot_code, user_ids, markdown, now_ms))


def _ready_config() -> PlatformImChannelConfig:
    return PlatformImChannelConfig(
        provider="DINGTALK",
        enabled=1,
        app_key="app-key",
        credential_ref="encrypted",
        robot_code="robot-code",
        base_url="https://api.dingtalk.com",
    )


def _token(expire_in: int = 7200) -> str:
    return json.dumps({"accessToken": "tok", "expireIn": expire_in}, separators=(",", ":"))


def test_dingtalk_http_error_keeps_safe_metadata_only() -> None:
    """状态码和安全标记留在消息里，原文和密钥不出现。"""
    raw = (
        '{"code":"authFailed","requestid":"req-123",'
        '"message":"secret and arbitrary provider response"}'
    )
    error = DingTalkHttpError(401, raw)
    assert error.status == 401
    assert error.provider_code == "authFailed"
    assert error.provider_request_id == "req-123"
    assert str(error) == "DingTalk request failed: HTTP 401 code=authFailed requestId=req-123"
    assert raw not in str(error)
    assert "secret" not in str(error)


def test_dingtalk_http_error_drops_unsafe_metadata() -> None:
    """带空格或尖括号的供应商标记不当作代码或请求号。"""
    error = DingTalkHttpError(
        500,
        '{"code":"bad code with spaces","requestid":"<unsafe>"}',
    )
    assert error.provider_code is None
    assert error.provider_request_id is None
    assert str(error) == "DingTalk request failed: HTTP 500"


def test_dingtalk_http_error_prefers_requestid_and_ignores_blank_bodies() -> None:
    """requestid 有内容时优先；空白正文没有供应商标记。"""
    preferred = DingTalkHttpError(
        400,
        '{"requestid":"   ","requestId":"req-9","code":401}',
    )
    assert preferred.provider_code == "401"
    assert preferred.provider_request_id == "req-9"
    blank = DingTalkHttpError(401, "  ")
    broken = DingTalkHttpError(401, "{not-json")
    assert str(blank) == "DingTalk request failed: HTTP 401"
    assert str(broken) == "DingTalk request failed: HTTP 401"


def test_dingtalk_exchange_turns_http_failure_into_typed_error() -> None:
    """非成功响应变成钉钉 HTTP 错误，正文里的密钥不进消息。"""
    body = '{"code":"authFailed","requestid":"req-401","message":"provider raw secret"}'

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = body.encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    with _Server(Handler) as server:
        exchange = HttpxDingTalkExchange()
        with pytest.raises(DingTalkHttpError) as caught:
            exchange.post(server.url + "/send", "{}", {})
    error = caught.value
    assert error.status == 401
    assert error.provider_code == "authFailed"
    assert "provider raw secret" not in str(error)


def test_dingtalk_exchange_wraps_transport_failure() -> None:
    """连不上时保留网络异常，供上层判断可以重试。"""
    exchange = HttpxDingTalkExchange()
    with pytest.raises(DingTalkTransportError) as caught:
        exchange.post("http://127.0.0.1:1/send", "{}", {})
    assert str(caught.value) == "DingTalk request transport failed"
    assert network_failure(caught.value) is True


def test_sender_caches_token_and_posts_single_markdown() -> None:
    """token 在过期前复用，单聊正文按 sampleMarkdown 发送。"""
    http = _Http([_token(), "{}"])
    sender = DingTalkOutboundSender(http)
    sender.send_single_markdown(
        "app-key",
        "secret-value",
        "https://api.dingtalk.com/",
        "robot-code",
        ["staff-001"],
        "markdown body",
        1_000,
    )
    sender.access_token("app-key", "secret-value", "https://api.dingtalk.com", 2_000)
    assert len(http.calls) == 2
    assert http.calls[0][0] == "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    assert http.calls[0][1] == '{"appKey":"app-key","appSecret":"secret-value"}'
    assert http.calls[0][2] == {}
    assert http.calls[1][0] == "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
    assert http.calls[1][2] == {"x-acs-dingtalk-access-token": "tok"}
    assert json.loads(http.calls[1][1]) == {
        "robotCode": "robot-code",
        "userIds": ["staff-001"],
        "msgKey": "sampleMarkdown",
        "msgParam": '{"title":"Auto Wonder","text":"markdown body"}',
    }


def test_sender_omits_null_secret_and_refreshes_when_inside_skew() -> None:
    """空密钥不写入正文；距过期不足 60 秒会重新申请。"""
    http = _Http([_token(120), _token(120)])
    sender = DingTalkOutboundSender(http)
    assert sender.access_token("app", None, None, 0) == "tok"
    assert http.calls[0][0] == DEFAULT_BASE_URL + "/v1.0/oauth2/accessToken"
    assert http.calls[0][1] == '{"appKey":"app"}'
    sender.access_token("app", None, "   ", 50_000)
    assert len(http.calls) == 1
    sender.access_token("app", None, None, 60_000)
    assert len(http.calls) == 2


def test_sender_rejects_incomplete_token_and_shapes_other_payloads() -> None:
    """token 缺字段时失败；群消息、webhook 和表情按各自字段发送。"""
    http = _Http(
        [
            '{"expireIn":10}',
            _token(),
            "{}",
            _token(),
            "{}",
            "{}",
            _token(),
            "{}",
        ]
    )
    sender = DingTalkOutboundSender(http)
    with pytest.raises(RuntimeError, match="incomplete"):
        sender.access_token("app", "secret", None, 0)
    sender.send_group_markdown("app", "secret", None, "robot", "conv", "md", ["u1"], 0)
    group = json.loads(http.calls[2][1])
    assert http.calls[2][0] == DEFAULT_BASE_URL + "/v1.0/robot/groupMessages/send"
    assert json.loads(group["msgParam"])["atUserIds"] == ["u1"]
    sender.send_markdown("app", "secret", None, "robot", "conv", "md", 8_000_000)
    plain = json.loads(http.calls[4][1])
    assert "atUserIds" not in json.loads(plain["msgParam"])
    sender.send_session_webhook_markdown("https://hook.example/send", "md", None)
    webhook = json.loads(http.calls[5][1])
    assert webhook["msgtype"] == "markdown"
    assert webhook["at"] == {"isAtAll": False, "atUserIds": []}
    sender.reply_thinking_emotion("app", "secret", None, "robot", "conv", "msg", 20_000_000)
    emotion = json.loads(http.calls[7][1])
    assert http.calls[7][0] == DEFAULT_BASE_URL + "/v1.0/robot/emotion/reply"
    assert emotion["emotionName"] == "🤔思考中"
    assert emotion["textEmotion"]["emotionId"] == "2659900"


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (503, True), (401, False)],
)
async def test_dingtalk_provider_classifies_http_status(status: int, retryable: bool) -> None:
    """429 和 5xx 可以重试，4xx 不行，消息里没有收件人。"""
    sender = _Sender()
    recipient = "staff-" + str(status)
    sender.error = DingTalkHttpError(
        status,
        '{"code":"provider-' + str(status) + '","requestid":"req-' + str(status) + '"}',
    )
    provider = DingTalkImProvider(_Configs(_ready_config(), "secret-value"), sender, lambda: 1)
    with pytest.raises(ImDeliveryError) as caught:
        await provider.send(ImSendCommand("DINGTALK", recipient, "title", "body"))
    error = caught.value
    assert error.retryable is retryable
    assert error.provider_code == "provider-" + str(status)
    assert error.provider_request_id == "req-" + str(status)
    assert recipient not in str(error)
    assert "secret-value" not in str(error)


async def test_dingtalk_provider_posts_current_credentials() -> None:
    """启用中的通道解密后，按时钟毫秒调用单聊发送。"""
    sender = _Sender()
    provider = DingTalkImProvider(
        _Configs(_ready_config(), "secret-value"),
        sender,
        lambda: 123456,
    )
    await provider.send(ImSendCommand("DINGTALK", "staff-001", "Title", "markdown body"))
    assert sender.calls == [
        (
            "app-key",
            "secret-value",
            "https://api.dingtalk.com",
            "robot-code",
            ["staff-001"],
            "markdown body",
            123456,
        )
    ]


async def test_dingtalk_provider_network_failure_is_retryable_and_redacted() -> None:
    """网络异常可以重试，收件人和密钥不出现在交付错误里。"""
    sender = _Sender()
    failure = RuntimeError("transport unavailable")
    failure.__cause__ = OSError("timeout")
    sender.error = failure
    provider = DingTalkImProvider(_Configs(_ready_config(), "secret-value"), sender, lambda: 1)
    with pytest.raises(ImDeliveryError) as caught:
        await provider.send(ImSendCommand("DINGTALK", "staff-network", "title", "body"))
    error = caught.value
    assert error.retryable is True
    assert error.provider_code == "transportFailure"
    assert "staff-network" not in str(error)
    assert "secret-value" not in str(error)
    assert str(error) == (
        "IM delivery failed provider=DINGTALK retryable=true "
        "providerCode=transportFailure providerRequestId=unknown"
    )


async def test_dingtalk_provider_rejects_incomplete_channel_before_send() -> None:
    """通道缺机器人编码或密钥为空时不调用发送器。"""
    incomplete = _ready_config()
    incomplete.robot_code = " "
    sender = _Sender()
    provider = DingTalkImProvider(_Configs(incomplete, "secret-value"), sender, lambda: 1)
    with pytest.raises(ImDeliveryError) as missing_robot:
        await provider.send(ImSendCommand("DINGTALK", "staff-001", "title", "body"))
    assert missing_robot.value.provider_code == "channelNotReady"
    assert sender.calls == []
    blank_secret = DingTalkImProvider(_Configs(_ready_config(), "  "), sender, lambda: 1)
    with pytest.raises(ImDeliveryError) as missing_secret:
        await blank_secret.send(ImSendCommand("DINGTALK", "staff-001", "title", "body"))
    assert missing_secret.value.provider_code == "channelNotReady"
    assert missing_secret.value.retryable is False


async def test_feishu_provider_authenticates_and_sends_rich_text() -> None:
    """先换 token，再按 user_id 发送含标题和正文的 post。"""
    captured = _FeishuCapture()
    configs = _Configs(_feishu_config(), "test-secret")
    with _FeishuServer(captured) as server, _feishu_http() as http:
        provider = FeishuImProvider(configs, http, server.url)
        await provider.send(ImSendCommand("FEISHU", "user-123", "指派提醒", "**新工单**"))
    auth = json.loads(captured.auth_request)
    assert auth["app_id"] == "cli_test"
    assert auth["app_secret"] == "test-secret"
    assert captured.authorization == "Bearer test-token"
    assert captured.query == "receive_id_type=user_id"
    body = json.loads(captured.message_request)
    assert body["receive_id"] == "user-123"
    assert body["msg_type"] == "post"
    assert "新工单" in body["content"]


async def test_feishu_provider_sanitizes_failures_and_retries_rate_limits() -> None:
    """HTTP 429 可以重试；业务码 10003 不可重试，响应原文不出现。"""
    captured = _FeishuCapture()
    captured.auth_status = 429
    captured.auth_body = '{"code":10003,"msg":"private secret"}'
    configs = _Configs(_feishu_config(), "secret")
    command = ImSendCommand("FEISHU", "user", "title", "body")
    with _FeishuServer(captured) as server, _feishu_http() as http:
        provider = FeishuImProvider(configs, http, server.url)
        with pytest.raises(ImDeliveryError) as limited:
            await provider.send(command)
        assert limited.value.retryable is True
        assert limited.value.provider_code == "http429"
        assert "private" not in str(limited.value)
        captured.auth_status = 200
        with pytest.raises(ImDeliveryError) as rejected:
            await provider.send(command)
    error = rejected.value
    assert error.retryable is False
    assert error.provider_code == "10003"
    assert "private" not in str(error)


@pytest.mark.parametrize("code", [99991400, 230020])
async def test_feishu_business_rate_limit_is_retryable(code: int) -> None:
    """飞书限流业务码可以重试，响应说明不进入错误。"""
    captured = _FeishuCapture()
    captured.auth_body = '{"code":' + str(code) + ',"msg":"private secret"}'
    with _FeishuServer(captured) as server, _feishu_http() as http:
        provider = FeishuImProvider(_Configs(_feishu_config(), "secret"), http, server.url)
        with pytest.raises(ImDeliveryError) as caught:
            await provider.send(ImSendCommand("FEISHU", "user", "title", "body"))
    assert caught.value.retryable is True
    assert caught.value.provider_code == str(code)
    assert "private" not in str(caught.value)


def test_registry_exposes_dingtalk_and_feishu() -> None:
    """测试发送会走到这两个供应商。"""
    registry = _registry(MemorySession())
    assert registry.require(" dingtalk ").provider() == "DINGTALK"
    assert registry.require("feishu").provider() == "FEISHU"


class _FeishuCapture:
    def __init__(self) -> None:
        self.auth_status = 200
        self.auth_body = '{"code":0,"tenant_access_token":"test-token"}'
        self.auth_request = ""
        self.authorization = ""
        self.query = ""
        self.message_request = ""


class _FeishuServer:
    def __init__(self, captured: _FeishuCapture) -> None:
        owner = captured

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                path, _, query = self.path.partition("?")
                if path == "/auth/v3/tenant_access_token/internal":
                    owner.auth_request = raw
                    text = owner.auth_body
                    status = owner.auth_status
                else:
                    owner.message_request = raw
                    owner.authorization = self.headers.get("Authorization", "")
                    owner.query = query
                    text = '{"code":0}'
                    status = 200
                payload = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return "http://" + str(host) + ":" + str(port)


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return "http://" + str(host) + ":" + str(port)


def _feishu_config() -> PlatformImChannelConfig:
    return PlatformImChannelConfig(provider="FEISHU", enabled=1, app_key="cli_test")


def _feishu_http() -> httpx.Client:
    return httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False)
