"""钉钉 HTTP 失败。消息只保留状态码和安全的供应商标记。"""

import json
import re

from autowonder.debuglogs.sanitizer import java_is_blank

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class DingTalkHttpError(Exception):
    """钉钉网关返回了非成功状态。"""

    def __init__(self, status: int, response_body: str | None) -> None:
        code, request_id = _metadata(response_body)
        self.status = status
        self.provider_code = code
        self.provider_request_id = request_id
        self._response_body = response_body
        super().__init__(_message(status, code, request_id))


class DingTalkTransportError(Exception):
    """钉钉请求没有拿到 HTTP 响应。原因链上保留原始网络异常。"""


def _message(status: int, code: str | None, request_id: str | None) -> str:
    text = "DingTalk request failed: HTTP " + str(status)
    if code is not None:
        text = text + " code=" + code
    if request_id is not None:
        text = text + " requestId=" + request_id
    return text


def _metadata(response_body: str | None) -> tuple[str | None, str | None]:
    if response_body is None or java_is_blank(response_body):
        return None, None
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    request_id = _first_non_blank(
        _string_field(parsed, "requestid"),
        _string_field(parsed, "requestId"),
    )
    return _safe(_string_field(parsed, "code")), _safe(request_id)


def _string_field(parsed: dict[str, object], key: str) -> str | None:
    value = parsed.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return str(value)


def _first_non_blank(first: str | None, second: str | None) -> str | None:
    if first is not None and not java_is_blank(first):
        return first
    return second


def _safe(value: str | None) -> str | None:
    if value is None or _SAFE_TOKEN.fullmatch(value) is None:
        return None
    return value
