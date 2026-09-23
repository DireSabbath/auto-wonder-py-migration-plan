"""健康检查与 hello，路径与 Java Controller 一致。"""

from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from autowonder.core.result import ok

router = APIRouter()


@router.get("/api/hello")
async def hello() -> dict[str, Any]:
    """连通性检查。"""
    return ok("Hello from AutoWonder!")


@router.api_route("/checkpreload.htm", methods=["GET", "POST", "HEAD"])
async def check_preload() -> PlainTextResponse:
    """预热探针。"""
    return PlainTextResponse("success")


@router.api_route("/status.taobao", methods=["GET", "POST", "HEAD"])
async def status_taobao() -> PlainTextResponse:
    """存活探针。进程能响应即视为成功。"""
    return PlainTextResponse("success")
