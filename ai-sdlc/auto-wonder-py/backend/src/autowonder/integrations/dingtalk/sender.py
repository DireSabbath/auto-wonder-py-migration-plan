"""钉钉机器人出站发送，并按应用缓存 access token。"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.integrations.dingtalk.errors import DingTalkHttpError, DingTalkTransportError

DEFAULT_BASE_URL = "https://api.dingtalk.com"
_TOKEN_SKEW_MS = 60_000
_DEFAULT_EXPIRE_MS = 7_200_000
_JAVA_LONG_MIN = -9223372036854775808
_JAVA_LONG_MAX = 9223372036854775807


class HttpExchange(Protocol):
    """POST JSON，返回响应体。非成功状态由实现抛出 ``DingTalkHttpError``。"""

    def post(self, url: str, json_body: str, headers: dict[str, str]) -> str:
        """发送一次请求。"""


@dataclass
class _TokenEntry:
    token: str
    expire_at_ms: int


class DingTalkOutboundSender:
    """机器人单聊、群聊、会话 webhook 和思考中表情。"""

    def __init__(self, http: HttpExchange) -> None:
        self._http = http
        self._token_cache: dict[tuple[str, str, str], _TokenEntry] = {}

    def access_token(
        self,
        app_key: str,
        app_secret: str | None,
        base_url: str | None,
        now_ms: int,
    ) -> str:
        """获取 access token。距过期不足 60 秒时重新申请。"""
        resolved = resolve_base_url(base_url)
        cache_key = (app_key, resolved, _fingerprint(app_secret))
        cached = self._token_cache.get(cache_key)
        if cached is not None and cached.expire_at_ms > now_ms + _TOKEN_SKEW_MS:
            return cached.token
        payload = {"appKey": app_key, "appSecret": app_secret}
        response = self._http.post(
            resolved + "/v1.0/oauth2/accessToken",
            compact_json(payload),
            {},
        )
        parsed = _object(response)
        token = _access_token(parsed)
        if token is None or java_is_blank(token):
            raise RuntimeError("DingTalk access token response is incomplete")
        entry = _TokenEntry(token, now_ms + _expire_millis(parsed.get("expireIn")))
        self._token_cache[cache_key] = entry
        return token

    def send_markdown(
        self,
        app_key: str,
        app_secret: str,
        base_url: str | None,
        robot_code: str,
        open_conversation_id: str,
        markdown: str,
        now_ms: int,
    ) -> None:
        """向群会话发送 markdown，不 @ 任何人。"""
        self.send_group_markdown(
            app_key,
            app_secret,
            base_url,
            robot_code,
            open_conversation_id,
            markdown,
            [],
            now_ms,
        )

    def send_session_webhook_markdown(
        self,
        session_webhook: str,
        markdown: str,
        at_user_ids: list[str] | None,
    ) -> None:
        """通过会话 webhook 发送 markdown。"""
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "Auto Wonder", "text": markdown},
            "at": {"isAtAll": False, "atUserIds": _at_user_ids(at_user_ids)},
        }
        self._http.post(session_webhook, compact_json(payload), {})

    def send_session_webhook_text(
        self,
        session_webhook: str,
        text: str,
        at_user_ids: list[str] | None,
    ) -> None:
        """通过会话 webhook 发送文本。"""
        payload = {
            "msgtype": "text",
            "text": {"content": text},
            "at": {"isAtAll": False, "atUserIds": _at_user_ids(at_user_ids)},
        }
        self._http.post(session_webhook, compact_json(payload), {})

    def send_group_markdown(
        self,
        app_key: str,
        app_secret: str,
        base_url: str | None,
        robot_code: str,
        open_conversation_id: str,
        markdown: str,
        at_user_ids: list[str] | None,
        now_ms: int,
    ) -> None:
        """向群会话发送 sampleMarkdown。有 @ 名单时写入 msgParam。"""
        param: dict[str, object] = {"title": "Auto Wonder", "text": markdown}
        self._send_group(
            app_key,
            app_secret,
            base_url,
            robot_code,
            open_conversation_id,
            "sampleMarkdown",
            param,
            at_user_ids,
            now_ms,
        )

    def send_group_text(
        self,
        app_key: str,
        app_secret: str,
        base_url: str | None,
        robot_code: str,
        open_conversation_id: str,
        text: str,
        at_user_ids: list[str] | None,
        now_ms: int,
    ) -> None:
        """向群会话发送 sampleText。"""
        param: dict[str, object] = {"content": text}
        self._send_group(
            app_key,
            app_secret,
            base_url,
            robot_code,
            open_conversation_id,
            "sampleText",
            param,
            at_user_ids,
            now_ms,
        )

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
        """给一批用户私聊发送 markdown。"""
        resolved = resolve_base_url(base_url)
        token = self.access_token(app_key, app_secret, base_url, now_ms)
        param = {"title": "Auto Wonder", "text": markdown}
        payload = {
            "robotCode": robot_code,
            "userIds": _at_user_ids(user_ids),
            "msgKey": "sampleMarkdown",
            "msgParam": compact_json(param),
        }
        self._http.post(
            resolved + "/v1.0/robot/oToMessages/batchSend",
            compact_json(payload),
            {"x-acs-dingtalk-access-token": token},
        )

    def reply_thinking_emotion(
        self,
        app_key: str,
        app_secret: str,
        base_url: str | None,
        robot_code: str,
        open_conversation_id: str,
        open_msg_id: str,
        now_ms: int,
    ) -> None:
        """回复思考中表情。"""
        self._send_thinking_emotion(
            app_key,
            app_secret,
            base_url,
            robot_code,
            open_conversation_id,
            open_msg_id,
            "/v1.0/robot/emotion/reply",
            now_ms,
        )

    def recall_thinking_emotion(
        self,
        app_key: str,
        app_secret: str,
        base_url: str | None,
        robot_code: str,
        open_conversation_id: str,
        open_msg_id: str,
        now_ms: int,
    ) -> None:
        """撤回思考中表情。"""
        self._send_thinking_emotion(
            app_key,
            app_secret,
            base_url,
            robot_code,
            open_conversation_id,
            open_msg_id,
            "/v1.0/robot/emotion/recall",
            now_ms,
        )

    def _send_group(
        self,
        app_key: str,
        app_secret: str,
        base_url: str | None,
        robot_code: str,
        open_conversation_id: str,
        msg_key: str,
        param: dict[str, object],
        at_user_ids: list[str] | None,
        now_ms: int,
    ) -> None:
        resolved = resolve_base_url(base_url)
        token = self.access_token(app_key, app_secret, base_url, now_ms)
        if at_user_ids is not None and len(at_user_ids) > 0:
            param["atUserIds"] = at_user_ids
        payload = {
            "robotCode": robot_code,
            "openConversationId": open_conversation_id,
            "msgKey": msg_key,
            "msgParam": compact_json(param),
        }
        self._http.post(
            resolved + "/v1.0/robot/groupMessages/send",
            compact_json(payload),
            {"x-acs-dingtalk-access-token": token},
        )

    def _send_thinking_emotion(
        self,
        app_key: str,
        app_secret: str,
        base_url: str | None,
        robot_code: str,
        open_conversation_id: str,
        open_msg_id: str,
        path: str,
        now_ms: int,
    ) -> None:
        resolved = resolve_base_url(base_url)
        token = self.access_token(app_key, app_secret, base_url, now_ms)
        text_emotion = {
            "emotionId": "2659900",
            "emotionName": "🤔思考中",
            "text": "🤔思考中",
            "backgroundId": "im_bg_1",
        }
        payload = {
            "robotCode": robot_code,
            "openConversationId": open_conversation_id,
            "openMsgId": open_msg_id,
            "emotionType": 2,
            "emotionName": "🤔思考中",
            "textEmotion": text_emotion,
        }
        self._http.post(
            resolved + path,
            compact_json(payload),
            {"x-acs-dingtalk-access-token": token},
        )


class HttpxDingTalkExchange:
    """对接钉钉网关。连接 5 秒，读取 10 秒，传输失败保留原因链。"""

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0, read=10.0, write=10.0, pool=5.0),
            follow_redirects=True,
            trust_env=False,
        )

    def post(self, url: str, json_body: str, headers: dict[str, str]) -> str:
        """POST JSON。2xx 返回正文，其他状态抛出 ``DingTalkHttpError``。"""
        request_headers = dict(headers)
        request_headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            response = self._client.post(
                url,
                content=json_body.encode("utf-8"),
                headers=request_headers,
            )
        except httpx.RequestError as error:
            raise DingTalkTransportError("DingTalk request transport failed") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise DingTalkHttpError(response.status_code, response.text)
        return response.text


_shared_sender: DingTalkOutboundSender | None = None


def shared_sender() -> DingTalkOutboundSender:
    """返回进程内唯一的发送器，token 缓存在这一份实例上。"""
    global _shared_sender
    if _shared_sender is None:
        _shared_sender = DingTalkOutboundSender(HttpxDingTalkExchange())
    return _shared_sender


def resolve_base_url(base_url: str | None) -> str:
    """空白地址用官方网关，只去掉末尾一个斜杠。"""
    if base_url is None or java_is_blank(base_url):
        return DEFAULT_BASE_URL
    if base_url.endswith("/"):
        return base_url[:-1]
    return base_url


def compact_json(value: object) -> str:
    """紧凑 JSON。空值不写出，对齐 Fastjson 默认序列化。"""
    return json.dumps(_skip_null(value), separators=(",", ":"), ensure_ascii=False)


def _skip_null(value: object) -> object:
    if isinstance(value, dict):
        kept: dict[str, object] = {}
        for key, item in value.items():
            if item is not None:
                kept[str(key)] = _skip_null(item)
        return kept
    if isinstance(value, list):
        copied: list[object] = []
        for item in value:
            copied.append(_skip_null(item))
        return copied
    return value


def network_failure(error: BaseException) -> bool:
    """原因链上出现网络或超时异常时可以重试。"""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, OSError | httpx.RequestError):
            return True
        current = current.__cause__
    return False


def _object(response: str) -> dict[str, object]:
    parsed = json.loads(response)
    if not isinstance(parsed, dict):
        raise RuntimeError("DingTalk access token response is incomplete")
    return parsed


def _access_token(parsed: dict[str, object]) -> str | None:
    value = parsed.get("accessToken")
    if isinstance(value, str):
        return value
    return None


def _expire_millis(value: object) -> int:
    seconds = _long_value(value)
    if seconds > 0:
        return seconds * 1000
    return _DEFAULT_EXPIRE_MS


def _long_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return _java_long(value)
    if isinstance(value, float) and value.is_integer():
        return _java_long(int(value))
    if isinstance(value, str) and _java_long_text(value):
        return _java_long(int(value))
    return 0


def _java_long(value: int) -> int:
    if value < _JAVA_LONG_MIN or value > _JAVA_LONG_MAX:
        return 0
    return value


def _java_long_text(value: str) -> bool:
    if value == "":
        return False
    digits = value
    if value[0] == "-":
        digits = value[1:]
    if digits == "":
        return False
    return digits.isdigit()


def _fingerprint(secret: str | None) -> str:
    text = ""
    if secret is not None:
        text = secret
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _at_user_ids(at_user_ids: list[str] | None) -> list[str]:
    if at_user_ids is None:
        return []
    return at_user_ids
