"""飞书协作通知。自定义应用按 user_id 发送富文本。"""

import json

import httpx

from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.im.providers import FEISHU, ImChannelGateway, ImDeliveryError, ImSendCommand

FEISHU_OPEN_API = "https://open.feishu.cn/open-apis"
_RETRYABLE_CODES = (99991400, 230020)

_shared_http: httpx.Client | None = None


def shared_feishu_http() -> httpx.Client:
    """进程内共享的飞书客户端。整次调用 10 秒，不跟随重定向。"""
    global _shared_http
    if _shared_http is None:
        _shared_http = httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False)
    return _shared_http


class FeishuImProvider:
    """向飞书用户发送 post 富文本。"""

    def __init__(self, configs: ImChannelGateway, http: httpx.Client, base_url: str) -> None:
        self._configs = configs
        self._http = http
        self._base_url = base_url

    def provider(self) -> str:
        """规范名称。"""
        return FEISHU

    async def send(self, command: ImSendCommand) -> None:
        """先换 tenant_access_token，再按 user_id 发消息。"""
        config = await self._configs.find_enabled(FEISHU)
        if config is None or config.app_key is None or java_is_blank(config.app_key):
            raise _failure(False, "channelNotReady")
        secret = self._configs.decrypt_secret(config)
        if secret is None or java_is_blank(secret):
            raise _failure(False, "channelNotReady")
        try:
            auth = self._exchange(
                "/auth/v3/tenant_access_token/internal",
                None,
                {"app_id": config.app_key, "app_secret": secret},
            )
            token = _as_text(auth.get("tenant_access_token"))
            if java_is_blank(token):
                raise _failure(False, "invalidTokenResponse")
            content = _compact(
                {
                    "zh_cn": {
                        "title": command.title,
                        "content": [[{"tag": "md", "text": command.markdown}]],
                    }
                }
            )
            self._exchange(
                "/im/v1/messages?receive_id_type=user_id",
                token,
                {
                    "receive_id": command.external_user_id,
                    "msg_type": "post",
                    "content": content,
                },
            )
        except ImDeliveryError:
            raise
        except (OSError, httpx.RequestError, json.JSONDecodeError, UnicodeDecodeError):
            # 传输异常可能夹带请求体。交付错误不挂原因链。
            raise _failure(True, "transportFailure") from None

    def _exchange(
        self,
        path: str,
        token: str | None,
        body: dict[str, object],
    ) -> dict[str, object]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        response = self._http.post(
            self._base_url + path,
            content=_compact(body).encode("utf-8"),
            headers=headers,
        )
        if response.status_code < 200 or response.status_code >= 300:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise _failure(retryable, "http" + str(response.status_code))
        parsed = json.loads(response.content)
        if not isinstance(parsed, dict):
            raise _failure(False, "invalidResponse")
        code = parsed.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise _failure(False, "invalidResponse")
        if code != 0:
            raise _failure(code in _RETRYABLE_CODES, str(code))
        return parsed


def _failure(retryable: bool, code: str) -> ImDeliveryError:
    return ImDeliveryError(FEISHU, retryable, code, None)


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
