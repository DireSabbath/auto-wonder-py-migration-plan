"""集成能力查询。该路径在鉴权白名单上，不要求登录。"""

from typing import Any

from fastapi import APIRouter

from autowonder.core.result import ok
from autowonder.integrations.capabilities import integration_capabilities

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("/capabilities")
async def capabilities() -> dict[str, Any]:
    """返回 Aone 是否启用。"""
    return ok(integration_capabilities())
