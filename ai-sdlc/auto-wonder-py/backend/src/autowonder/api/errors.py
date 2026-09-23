"""把业务异常写成 Result 信封，状态码对齐 ``GlobalExceptionHandler``。"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from autowonder.api.access import access_denied_body
from autowonder.core.errors import BizError, ErrorCode, WorkspaceAccessDenied
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


def install_exception_handlers(app: FastAPI) -> None:
    """注册与 Java advice 相同的异常到 HTTP 映射。"""

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

    @app.exception_handler(IntegrityError)
    async def handle_integrity(_request: Request, _exc: IntegrityError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=fail(ErrorCode.CONFLICT, "数据冲突，请刷新页面后重试"),
        )
