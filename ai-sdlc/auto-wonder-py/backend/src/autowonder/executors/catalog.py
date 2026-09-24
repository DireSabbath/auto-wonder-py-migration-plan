"""供应商模型目录快照。读取 Redis 里已经刷好的快照，不在请求里触发刷新。"""

import json
import logging
from datetime import datetime

from autowonder.core.clock import SHANGHAI
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.redis import redis_client
from autowonder.executors.options import FALLBACK_MODELS, ModelOption, is_qoder_family
from autowonder.executors.schemas import ProviderModelCatalogItemView, ProviderModelCatalogView

logger = logging.getLogger(__name__)

_SNAPSHOT_PREFIX = "model-catalog:snapshot:"
_SUPPORTED = {"qoder", "qodercn"}


def require_supported_provider(provider: str) -> None:
    """目录只接受 qoder 和 qodercn。"""
    if provider not in _SUPPORTED:
        raise BizError(ErrorCode.PARAM_INVALID, "仅支持 qoder 或 qodercn provider")


def parse_catalog_snapshot(provider: str, raw: str | None) -> ProviderModelCatalogView | None:
    """解析一份快照。空白、坏 JSON 或校验不通过时当作没有快照。"""
    if raw is None or raw.strip() == "":
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Provider model catalog snapshot is invalid provider=%s", provider)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("provider") != provider:
        return None
    source_executor_id = payload.get("sourceExecutorId")
    if not isinstance(source_executor_id, int) or isinstance(source_executor_id, bool):
        return None
    if source_executor_id <= 0:
        return None
    last_successful_at = _millis(payload.get("lastSuccessfulAt"))
    if last_successful_at is None:
        return None
    models = _models(payload.get("models"))
    if models is None:
        return None
    return ProviderModelCatalogView(
        provider=provider,
        models=models,
        last_successful_at=last_successful_at,
    )


async def read_catalog(provider: str) -> ProviderModelCatalogView:
    """读取一个 provider 的目录。没有可用快照时模型列表为空。"""
    require_supported_provider(provider)
    raw = await redis_client().get(_SNAPSHOT_PREFIX + provider)
    parsed = parse_catalog_snapshot(provider, raw)
    if parsed is None:
        return ProviderModelCatalogView(provider=provider, models=[], last_successful_at=None)
    return parsed


async def model_options(provider: str) -> list[ModelOption]:
    """有活目录就用目录，否则用页面同一份内置列表。非 Qoder 没有模型。"""
    if not is_qoder_family(provider):
        return []
    try:
        catalog = await read_catalog(provider)
    except Exception:
        logger.warning(
            "provider model catalog unavailable provider=%s, falling back to static models",
            provider,
            exc_info=True,
        )
        return list(FALLBACK_MODELS)
    options = [ModelOption(item.id, item.name) for item in catalog.models]
    if not options:
        return list(FALLBACK_MODELS)
    return options


async def catalog_names(provider: str) -> dict[str, str]:
    """模型 id 到展示名。目录读失败时返回空表，列表接口仍然成功。"""
    try:
        catalog = await read_catalog(provider)
    except Exception:
        logger.debug("model catalog lookup failed provider=%s", provider, exc_info=True)
        return {}
    names: dict[str, str] = {}
    for item in catalog.models:
        if item.id not in names:
            names[item.id] = item.name
    return names


def _models(raw: object) -> list[ProviderModelCatalogItemView] | None:
    if not isinstance(raw, list) or not raw:
        return None
    seen: set[str] = set()
    items: list[ProviderModelCatalogItemView] = []
    for raw_item in raw:
        if not isinstance(raw_item, dict):
            return None
        item_id = raw_item.get("id")
        item_name = raw_item.get("name")
        if not isinstance(item_id, str) or not isinstance(item_name, str):
            return None
        trimmed_id = item_id.strip()
        trimmed_name = item_name.strip()
        if trimmed_id == "" or trimmed_name == "" or trimmed_id in seen:
            return None
        seen.add(trimmed_id)
        items.append(ProviderModelCatalogItemView(id=item_id, name=item_name))
    return items


def _millis(value: object) -> datetime | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000, SHANGHAI).replace(tzinfo=None)
