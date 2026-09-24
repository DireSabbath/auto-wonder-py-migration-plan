"""健康检查与 hello，路径与 Java Controller 一致。"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from autowonder.core.result import ok

router = APIRouter()

# Java 用 classpath 上的 META-INF/resources/status.taobao 判断进程是否仍在服务。
STATUS_MARKER = Path(__file__).resolve().parents[2] / "META-INF" / "resources" / "status.taobao"
_STATUS_MISSING = (
    "HealthCheckController can not found META-INF/resources/status.taobao, "
    "please check app status.; server maybe in rebooting..."
)


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
    """存活探针。标记文件不在时与 Java 一样返回 404。"""
    if not STATUS_MARKER.is_file():
        return PlainTextResponse(_STATUS_MISSING, status_code=404)
    return PlainTextResponse("success")
