"""钉钉协作通知。凭证来自当前启用的平台通道。"""

import time
from collections.abc import Callable

from autowonder.debuglogs.sanitizer import java_is_blank
from autowonder.im.models import PlatformImChannelConfig
from autowonder.im.providers import DINGTALK, ImChannelGateway, ImDeliveryError, ImSendCommand
from autowonder.integrations.dingtalk.errors import DingTalkHttpError
from autowonder.integrations.dingtalk.sender import DingTalkOutboundSender, network_failure


def system_now_ms() -> int:
    """当前 UTC 纪元毫秒。"""
    return time.time_ns() // 1_000_000


class DingTalkImProvider:
    """用钉钉机器人单聊发送协作通知。"""

    def __init__(
        self,
        configs: ImChannelGateway,
        sender: DingTalkOutboundSender,
        now_ms: Callable[[], int],
    ) -> None:
        self._configs = configs
        self._sender = sender
        self._now_ms = now_ms

    def provider(self) -> str:
        """规范名称。"""
        return DINGTALK

    async def send(self, command: ImSendCommand) -> None:
        """通道不完整时拒绝；HTTP 429 和 5xx 可以重试。"""
        config = await self._configs.find_enabled(DINGTALK)
        fields = _ready_fields(config)
        if fields is None:
            raise ImDeliveryError(DINGTALK, False, "channelNotReady", None)
        app_key, robot_code, base_url = fields
        try:
            secret = self._configs.decrypt_secret(config)
            if secret is None or java_is_blank(secret):
                raise ImDeliveryError(DINGTALK, False, "channelNotReady", None)
            self._sender.send_single_markdown(
                app_key,
                secret,
                base_url,
                robot_code,
                [command.external_user_id],
                command.markdown,
                self._millis(),
            )
        except ImDeliveryError:
            raise
        except DingTalkHttpError as error:
            retryable = error.status == 429 or error.status >= 500
            raise ImDeliveryError(
                DINGTALK,
                retryable,
                error.provider_code,
                error.provider_request_id,
            ) from error
        except Exception as error:
            # 网络失败可以重试。交付消息只保留安全标记，不带上原始异常文本。
            raise ImDeliveryError(
                DINGTALK,
                network_failure(error),
                "transportFailure",
                None,
            ) from error

    def _millis(self) -> int:
        return self._now_ms()


def _ready_fields(
    config: PlatformImChannelConfig | None,
) -> tuple[str, str, str | None] | None:
    if config is None or config.enabled != 1:
        return None
    app_key = config.app_key
    credential = config.credential_ref
    robot_code = config.robot_code
    if app_key is None or java_is_blank(app_key):
        return None
    if credential is None or java_is_blank(credential):
        return None
    if robot_code is None or java_is_blank(robot_code):
        return None
    return app_key, robot_code, config.base_url
