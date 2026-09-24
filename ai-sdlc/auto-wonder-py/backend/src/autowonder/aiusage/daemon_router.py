"""执行器上报任务用量。这条路径在鉴权白名单里，靠执行器令牌校验。"""

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.aiusage.dispatch_usage import record_task_usage
from autowonder.artifacts.daemon_auth import authenticate
from autowonder.db.session import get_session
from autowonder.debuglogs.sanitizer import java_is_blank

router = APIRouter(prefix="/api/daemon", tags=["daemon-task-usage"])

_LONG_MIN = -9223372036854775808
_LONG_MAX = 9223372036854775807
_DISPATCH_ID = re.compile(r"[+-]?\d+")
_BEARER = "Bearer "


class TaskUsageEntry(BaseModel):
    """单条任务用量。JSON 字段名与 Java 注解保持 snake_case。"""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    provider: str | None = None
    model: str | None = None
    step_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    credits: float | None = None


class TaskUsageReportRequest(BaseModel):
    """执行器上报的用量列表。缺省表示没有条目。"""

    model_config = ConfigDict(extra="ignore")

    usage: list[TaskUsageEntry] | None = None


def parse_dispatch_id(text: str) -> int | None:
    """按 Java ``Long.parseLong`` 接受可选符号和十进制数字，范围是有符号 64 位。"""
    if _DISPATCH_ID.fullmatch(text) is None:
        return None
    value = int(text)
    if value < _LONG_MIN or value > _LONG_MAX:
        return None
    return value


def header_token(authorization: str | None) -> str | None:
    """``Bearer `` 前缀则去掉前缀，其余原文使用。没有头时返回空。"""
    if authorization is None:
        return None
    if authorization.startswith(_BEARER):
        return authorization[len(_BEARER) :]
    return authorization


def usage_authorization_token(token: str | None, authorization: str | None) -> str | None:
    """查询参数里的非空白令牌优先，否则取 Authorization。"""
    if token is not None and not java_is_blank(token):
        return token
    return header_token(authorization)


def usage_entries(body: TaskUsageReportRequest | None) -> list[dict[str, Any]] | None:
    """空请求不产出条目。"""
    if body is None:
        return None
    if body.usage is None:
        return None
    return [item.model_dump(exclude_none=True) for item in body.usage]


def chosen_dispatch_text(task_id: str, dispatch_id_param: str | None) -> str:
    """非空白的 ``dispatchId`` 覆盖路径里的任务号。"""
    if dispatch_id_param is None:
        return task_id
    if java_is_blank(dispatch_id_param):
        return task_id
    return dispatch_id_param


async def report_task_usage(
    session: AsyncSession,
    task_id: str,
    dispatch_id_param: str | None,
    token: str | None,
    authorization: str | None,
    entries: list[dict[str, Any]] | None,
) -> tuple[int, dict[str, str] | None]:
    """非法调度号返回 400。令牌缺失或校验失败返回空 401。成功正文是 accepted。"""
    dispatch_id = parse_dispatch_id(chosen_dispatch_text(task_id, dispatch_id_param))
    if dispatch_id is None:
        return 400, {"error": "invalid dispatch id"}
    auth_token = usage_authorization_token(token, authorization)
    if auth_token is None:
        return 401, None
    if java_is_blank(auth_token):
        return 401, None
    auth = await authenticate(session, dispatch_id, auth_token)
    if not auth.success:
        return 401, None
    await record_task_usage(session, auth.tenant_id, dispatch_id, entries)
    return 200, {"status": "accepted"}


def usage_http_response(status: int, body: dict[str, str] | None) -> Response:
    """失败且没有正文时保持空响应。"""
    if body is None:
        return Response(status_code=status)
    return JSONResponse(content=body, status_code=status)


@router.post("/tasks/{taskId}/usage")
async def report_usage(
    taskId: str,
    dispatchId: Annotated[str | None, Query()] = None,
    token: Annotated[str | None, Query()] = None,
    authorization: Annotated[str | None, Header()] = None,
    body: TaskUsageReportRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """接受执行器上报的任务用量。"""
    status, payload = await report_task_usage(
        session,
        taskId,
        dispatchId,
        token,
        authorization,
        usage_entries(body),
    )
    return usage_http_response(status, payload)
