"""平台自动升级开关。任何已登录用户看到同一份部署配置，没有写接口。"""

from typing import Any

from fastapi import APIRouter

from autowonder.core.result import ok
from autowonder.executors.updates import runtime_auto_update_view

router = APIRouter(tags=["runtime-auto-update"])


@router.get("/api/platform/runtime-auto-update")
async def runtime_auto_update() -> dict[str, Any]:
    """返回全局执行器自动升级开关和目标版本。"""
    return ok(runtime_auto_update_view())
