"""把业务异常写成 Result 信封，状态码对齐 ``GlobalExceptionHandler``。"""

import inspect
from collections.abc import Mapping, Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from autowonder.api.access import access_denied_body
from autowonder.core.errors import (
    BizError,
    ErrorCode,
    IllegalArgumentError,
    WorkspaceAccessDenied,
)
from autowonder.core.result import fail


def _biz_status(code: str) -> int:
    if code == ErrorCode.UNAUTHORIZED.code:
        return 401
    if code in {ErrorCode.WORKSPACE_NOT_MEMBER.code, ErrorCode.NO_PERMISSION.code}:
        return 403
    if code in {ErrorCode.PARAM_INVALID.code, ErrorCode.WORKSPACE_ACCESS_LEVEL_INVALID.code}:
        return 400
    if code in {
        ErrorCode.CONFLICT.code,
        ErrorCode.WORKSPACE_OWNER_MUTATION_PROTECTED.code,
        ErrorCode.WORKSPACE_SELF_LEVEL_MUTATION_FORBIDDEN.code,
        ErrorCode.WORKSPACE_OWNER_TRANSFER_INVALID.code,
    }:
        return 409
    return 200


def _validation_response(request: Request, exc: RequestValidationError) -> JSONResponse:
    """缺正文与坏 JSON 是 400；字段校验带字段名；查询、表单和文件落到 10000。"""
    errors = exc.errors()
    if _body_unreadable(errors):
        return JSONResponse(status_code=400, content=fail(ErrorCode.PARAM_INVALID))
    if _non_body(errors) or _form_endpoint(request):
        return JSONResponse(status_code=200, content=fail(ErrorCode.SYSTEM_ERROR))
    field = _body_field(errors)
    if field is not None:
        return JSONResponse(
            status_code=400,
            content=fail(ErrorCode.PARAM_INVALID, field + " 参数不合法"),
        )
    return JSONResponse(status_code=200, content=fail(ErrorCode.SYSTEM_ERROR))


def _body_unreadable(errors: Sequence[Mapping[str, object]]) -> bool:
    for error in errors:
        if error.get("type") == "json_invalid":
            return True
        if error.get("loc") == ("body",):
            return True
    return False


def _non_body(errors: Sequence[Mapping[str, object]]) -> bool:
    for error in errors:
        loc = error.get("loc")
        if isinstance(loc, tuple) and loc and loc[0] in {"query", "header", "path", "cookie"}:
            return True
    return False


def _body_field(errors: Sequence[Mapping[str, object]]) -> str | None:
    for error in errors:
        loc = error.get("loc")
        if isinstance(loc, tuple) and len(loc) >= 2 and loc[0] == "body":
            return str(loc[1])
    return None


def _form_endpoint(request: Request) -> bool:
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return False
    for param in inspect.signature(endpoint).parameters.values():
        rendered = repr(param.annotation)
        if "UploadFile" in rendered or "Form(" in rendered or "File(" in rendered:
            return True
    return False


def install_exception_handlers(app: FastAPI) -> None:
    """注册与 Java advice 相同的异常到 HTTP 映射。"""

    @app.exception_handler(IllegalArgumentError)
    async def handle_illegal(_request: Request, exc: IllegalArgumentError) -> JSONResponse:
        return JSONResponse(
            status_code=200,
            content=fail(ErrorCode.PARAM_INVALID, str(exc)),
        )

    @app.exception_handler(BizError)
    async def handle_biz(_request: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(
            status_code=_biz_status(exc.code),
            content=fail(exc.error_code, str(exc)),
        )

    @app.exception_handler(WorkspaceAccessDenied)
    async def handle_access(_request: Request, exc: WorkspaceAccessDenied) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content=fail(
                ErrorCode.WORKSPACE_ACCESS_INSUFFICIENT,
                str(exc),
                access_denied_body(exc),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _validation_response(request, exc)

    @app.exception_handler(IntegrityError)
    async def handle_integrity(_request: Request, _exc: IntegrityError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=fail(ErrorCode.CONFLICT, "数据冲突，请刷新页面后重试"),
        )
