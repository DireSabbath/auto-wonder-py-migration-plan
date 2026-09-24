"""CLI 需求文档接口。会话过滤器按精确路径放行，这里改用短令牌。"""

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Header, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from autowonder.artifacts.cli_tokens import (
    authenticate_download,
    authenticate_upload,
    load_scheduled_task,
    load_workitem,
    presented_token,
    require_read_membership,
    require_write_membership,
)
from autowonder.artifacts.documents import (
    ArtifactOwner,
    list_requirement_documents,
    read_requirement_document,
    upload_named_files,
    workitem_owner,
)
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import fail, fail_omitting_nulls, ok
from autowonder.db.session import get_session
from autowonder.workitems.models import Workitem

router = APIRouter()


def upload_status(code: str) -> int:
    """工单 CLI 上传把业务码映射成 HTTP 状态。"""
    if code == ErrorCode.UNAUTHORIZED.code:
        return 401
    if code in {ErrorCode.NO_PERMISSION.code, ErrorCode.WORKSPACE_NOT_MEMBER.code}:
        return 403
    if code in {ErrorCode.WORKITEM_NOT_FOUND.code, ErrorCode.ARTIFACT_NOT_FOUND.code}:
        return 404
    if code == ErrorCode.CONFLICT.code:
        return 409
    if code == ErrorCode.PARAM_INVALID.code:
        return 400
    return 500


def download_status(code: str) -> int:
    """工单 CLI 下载把业务码映射成 HTTP 状态。冲突仍按服务器错误。"""
    if code == ErrorCode.UNAUTHORIZED.code:
        return 401
    if code in {ErrorCode.NO_PERMISSION.code, ErrorCode.WORKSPACE_NOT_MEMBER.code}:
        return 403
    if code in {ErrorCode.WORKITEM_NOT_FOUND.code, ErrorCode.ARTIFACT_NOT_FOUND.code}:
        return 404
    if code == ErrorCode.PARAM_INVALID.code:
        return 400
    return 500


def scheduled_upload_status(code: str) -> int:
    """定时任务 CLI 上传把缺失和归档状态映射成 404 与 409。"""
    if code == ErrorCode.UNAUTHORIZED.code:
        return 401
    if code in {ErrorCode.NO_PERMISSION.code, ErrorCode.WORKSPACE_NOT_MEMBER.code}:
        return 403
    if code in {ErrorCode.SCHEDULED_TASK_NOT_FOUND.code, ErrorCode.ARTIFACT_NOT_FOUND.code}:
        return 404
    if code in {ErrorCode.CONFLICT.code, ErrorCode.SCHEDULED_TASK_INVALID_STATE.code}:
        return 409
    if code == ErrorCode.PARAM_INVALID.code:
        return 400
    return 500


@router.post("/api/cli/workitems/{workitemId}/requirement-documents")
async def upload_workitem_documents(
    workitemId: int,
    files: list[UploadFile] = File(),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """用上传令牌保存工单需求文档，并再次核对写权限。"""
    try:
        user_id = authenticate_upload(presented_token(authorization))
        workitem = await load_workitem(session, workitemId)
        await require_write_membership(session, workitem.tenant_id, user_id)
        payload: list[tuple[str | None, bytes]] = []
        for item in files:
            payload.append((item.filename, await item.read()))
        views = await upload_named_files(
            session,
            workitem_owner(workitemId),
            payload,
            workitem.tenant_id,
            user_id,
            "CLI",
        )
    except BizError as error:
        return _json_fail(error, upload_status(error.code))
    return JSONResponse(content=ok(views))


@router.get("/api/cli/workitems/{workitemId}/requirement-documents/index")
async def list_workitem_documents(
    workitemId: int,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """用下载令牌列出工单需求文档。"""
    try:
        workitem = await _readable_workitem(session, workitemId, authorization)
        views = await list_requirement_documents(
            session,
            workitem_owner(workitemId),
            workitem.tenant_id,
        )
    except BizError as error:
        return _json_fail(error, download_status(error.code))
    return JSONResponse(content=ok(views))


@router.get("/api/cli/workitems/{workitemId}/requirement-documents/{artifactId}/content")
async def read_workitem_document(
    workitemId: int,
    artifactId: int,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """用下载令牌返回原始文件。失败体是省略空字段的 JSON。"""
    try:
        workitem = await _readable_workitem(session, workitemId, authorization)
        content = await read_requirement_document(
            session,
            workitemId,
            artifactId,
            workitem.tenant_id,
        )
    except BizError as error:
        return Response(
            content=_fastjson_fail(error),
            status_code=download_status(error.code),
            headers={
                "Content-Type": "application/json",
                "X-Content-Type-Options": "nosniff",
            },
        )
    return Response(
        content=content.payload,
        headers={
            "Content-Disposition": _attachment(content.filename),
            "Content-Type": content.content_type,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/api/cli/scheduled-tasks/{taskId}/documents")
async def upload_scheduled_task_documents(
    taskId: int,
    files: list[UploadFile] = File(),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """用上传令牌保存定时任务需求文档，并再次核对写权限。"""
    try:
        user_id = authenticate_upload(presented_token(authorization))
        task = await load_scheduled_task(session, taskId)
        await require_write_membership(session, task.workspace_id, user_id)
        payload: list[tuple[str | None, bytes]] = []
        for item in files:
            payload.append((item.filename, await item.read()))
        views = await upload_named_files(
            session,
            ArtifactOwner("SCHEDULED_TASK", taskId),
            payload,
            task.workspace_id,
            user_id,
            "CLI",
        )
    except BizError as error:
        return _json_fail(error, scheduled_upload_status(error.code))
    return JSONResponse(content=ok(views))


async def _readable_workitem(
    session: AsyncSession,
    workitem_id: int,
    authorization: str | None,
) -> Workitem:
    user_id = authenticate_download(presented_token(authorization))
    workitem = await load_workitem(session, workitem_id)
    await require_read_membership(session, workitem.tenant_id, user_id)
    return workitem


def _json_fail(error: BizError, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=fail(error.error_code, str(error)),
    )


def _fastjson_fail(error: BizError) -> bytes:
    body = fail_omitting_nulls(error.error_code, str(error))
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _attachment(filename: str) -> str:
    escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
    encoded = quote(filename, safe="")
    return 'attachment; filename="' + escaped + "\"; filename*=UTF-8''" + encoded
