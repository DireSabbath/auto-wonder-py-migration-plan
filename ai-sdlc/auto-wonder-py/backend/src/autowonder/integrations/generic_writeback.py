"""没有专用适配器的外部工单，用通用 HTTP 回写标题和正文。"""

import json
from urllib.parse import quote

import httpx

from autowonder.integrations.aone_api import AoneConfig
from autowonder.integrations.aone_codec import AoneOpenApiError

_CLIENT = httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0))


def update_generic_content(
    provider: str | None,
    config: AoneConfig,
    external_workitem_id: str | None,
    title: str | None,
    content_md: str | None,
) -> None:
    """PUT ``/api/workitems/{id}/content``。连接失败保留原始网络异常作为原因。"""
    if config.base_url.strip() == "":
        raise AoneOpenApiError("Generic writeback baseUrl is required")
    if external_workitem_id is None or external_workitem_id.strip() == "":
        raise AoneOpenApiError("Generic writeback externalWorkitemId is required")
    body = json.dumps(
        {
            "provider": provider,
            "externalWorkitemId": external_workitem_id,
            "title": title,
            "contentMd": content_md,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    provider_header = ""
    if provider is not None:
        provider_header = provider
    headers = {"X-AutoWonder-Provider": provider_header}
    if config.client_key.strip() != "":
        headers["X-AutoWonder-Client-Key"] = config.client_key
    if config.access_secret.strip() != "":
        headers["Authorization"] = "Bearer " + config.access_secret
    url = (
        _trim_slash(config.base_url)
        + "/api/workitems/"
        + quote(external_workitem_id, safe="")
        + "/content"
    )
    try:
        response = _CLIENT.put(url, content=body.encode(), headers=headers)
    except httpx.HTTPError as error:
        raise AoneOpenApiError("Generic writeback request failed: " + str(error)) from error
    if response.is_success:
        return
    text = response.text
    detail = "Generic writeback failed: HTTP " + str(response.status_code)
    if text.strip() != "":
        detail = detail + " " + text
    raise AoneOpenApiError(detail)


def _trim_slash(value: str) -> str:
    result = value.strip()
    while result.endswith("/"):
        result = result[: len(result) - 1]
    return result
