"""参数绑定先于工作空间切面。

Spring 先解析路径、查询、请求头和正文。绑定失败时不会进入
``WorkspaceAccessAspect``，因此缺正文是 400，缺查询落到 10000。
绑定成功后，依赖里的工作空间检查才返回 11001。
直接读 ``Request`` 的写接口同样先要求 JSON 对象，飞书回调和 daemon 除外。
"""

import json
from typing import Any

from fastapi import Request, WebSocket
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import (
    SolvedDependency,
    request_body_to_args,
    request_params_to_args,
)
from starlette.background import BackgroundTasks
from starlette.responses import Response

_installed = False


def install_binding_before_access() -> None:
    """让路由先收集绑定错误，再执行依赖。"""
    global _installed
    if _installed:
        return
    import fastapi.routing as routing

    original = routing.solve_dependencies

    async def solve_binding_first(
        *,
        request: Request | WebSocket,
        dependant: Dependant,
        body: Any = None,
        background_tasks: BackgroundTasks | None = None,
        response: Response | None = None,
        dependency_overrides_provider: Any = None,
        dependency_cache: dict[Any, Any] | None = None,
        async_exit_stack: Any,
        embed_body_fields: bool,
    ) -> SolvedDependency:
        errors = await _binding_errors(request, dependant, body, embed_body_fields)
        if errors:
            if response is None:
                response = Response()
                del response.headers["content-length"]
                response.status_code = None  # type: ignore[assignment]
            if dependency_cache is None:
                dependency_cache = {}
            return SolvedDependency(
                values={},
                errors=errors,
                background_tasks=background_tasks,
                response=response,
                dependency_cache=dependency_cache,
            )
        return await original(
            request=request,
            dependant=dependant,
            body=body,
            background_tasks=background_tasks,
            response=response,
            dependency_overrides_provider=dependency_overrides_provider,
            dependency_cache=dependency_cache,
            async_exit_stack=async_exit_stack,
            embed_body_fields=embed_body_fields,
        )

    routing.solve_dependencies = solve_binding_first
    _installed = True


async def _binding_errors(
    request: Request | WebSocket,
    dependant: Dependant,
    body: Any,
    embed_body_fields: bool,
) -> list[Any]:
    errors: list[Any] = []
    _, path_errors = request_params_to_args(dependant.path_params, request.path_params)
    _, query_errors = request_params_to_args(dependant.query_params, request.query_params)
    _, header_errors = request_params_to_args(dependant.header_params, request.headers)
    _, cookie_errors = request_params_to_args(dependant.cookie_params, request.cookies)
    errors.extend(path_errors)
    errors.extend(query_errors)
    errors.extend(header_errors)
    errors.extend(cookie_errors)
    if dependant.body_params:
        _, body_errors = await request_body_to_args(
            body_fields=dependant.body_params,
            received_body=body,
            embed_body_fields=embed_body_fields,
        )
        errors.extend(body_errors)
    if not errors:
        errors.extend(await _raw_json_body_errors(request, dependant))
    return errors


def _missing_body() -> dict[str, object]:
    return {"type": "missing", "loc": ("body",), "msg": "Field required", "input": None}


def _invalid_json(error: json.JSONDecodeError) -> dict[str, object]:
    return {
        "type": "json_invalid",
        "loc": ("body", error.pos),
        "msg": "JSON decode error",
        "input": {},
        "ctx": {"error": error.msg},
    }


async def _raw_json_body_errors(
    request: Request | WebSocket,
    dependant: Dependant,
) -> list[dict[str, object]]:
    """没有声明正文模型、但方法自己读 JSON 时，空正文和坏 JSON 先于工作空间检查。"""
    if dependant.body_params or dependant.request_param_name is None:
        return []
    if not isinstance(request, Request):
        return []
    if request.method not in {"POST", "PUT", "PATCH"}:
        return []
    path = request.url.path
    if path.startswith("/api/daemon/") or path.startswith("/api/integrations/feishu/callback"):
        return []
    raw = await request.body()
    if raw == b"":
        return [_missing_body()]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        return [_invalid_json(error)]
    if not isinstance(parsed, dict):
        return [_missing_body()]
    return []
