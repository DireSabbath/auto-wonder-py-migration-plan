"""参数绑定先于工作空间切面。

Spring 先解析路径、查询、请求头和正文。绑定失败时不会进入
``WorkspaceAccessAspect``，因此缺正文是 400，缺查询落到 10000。
绑定成功后，依赖里的工作空间检查才返回 11001。
"""

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
    return errors
