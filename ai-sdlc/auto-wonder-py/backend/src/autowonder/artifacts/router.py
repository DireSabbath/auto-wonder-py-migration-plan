"""工作项产物列表、下载、预览，以及需求文档。"""

from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.artifacts.documents import (
    delete_requirement_document,
    list_requirement_documents,
    upload_named_files,
    workitem_owner,
)
from autowonder.artifacts.service import (
    content_type,
    download_url,
    is_html,
    list_by_workitem,
    preview_bytes,
    preview_status,
)
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import ok
from autowonder.db.session import get_session

router = APIRouter(
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看工作项文档"))],
)


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


@router.get("/api/workitems/{id}/artifacts")
async def list_workitem_artifacts(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """当前工作空间该工单的用户可见产物。"""
    return ok(await list_by_workitem(session, _workspace_id(), id))


@router.get("/api/workitems/{id}/requirement-documents")
async def list_requirement_docs(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """当前工作空间该工单的需求文档。"""
    return ok(await list_requirement_documents(session, workitem_owner(id), _workspace_id()))


@router.post(
    "/api/workitems/{id}/requirement-documents",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "上传需求文档"))],
)
async def upload_requirement_docs(
    id: int,
    files: list[UploadFile] = File(),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按表单顺序保存需求文档。"""
    payload: list[tuple[str | None, bytes]] = []
    for item in files:
        payload.append((item.filename, await item.read()))
    views = await upload_named_files(
        session,
        workitem_owner(id),
        payload,
        _workspace_id(),
        _user_id(),
        "WEB",
    )
    return ok(views)


@router.delete(
    "/api/workitems/{id}/requirement-documents/{artifactId}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除需求文档"))],
)
async def delete_requirement_doc(
    id: int,
    artifactId: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """删除一份需求文档。"""
    await delete_requirement_document(
        session,
        workitem_owner(id),
        artifactId,
        _workspace_id(),
        _user_id(),
    )
    return ok(True)


@router.get("/api/artifacts/{id}/download")
async def download_artifact(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """返回预签名下载地址。"""
    return ok(await download_url(session, id, _workspace_id()))


@router.get("/api/artifacts/{id}/preview")
async def preview_artifact(
    id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """按扩展名返回正文。HTML 带 sandbox，失败时是纯文本而不是 Result。"""
    try:
        name, payload = await preview_bytes(session, id, _workspace_id())
    except BizError as error:
        return Response(
            content=str(error).encode("utf-8"),
            status_code=preview_status(error.code),
            headers={
                "Content-Type": "text/plain",
                "X-Content-Type-Options": "nosniff",
            },
        )
    headers = {
        "Content-Type": content_type(name),
        "X-Content-Type-Options": "nosniff",
    }
    if is_html(name):
        headers["Content-Security-Policy"] = "sandbox"
    return Response(content=payload, headers=headers)
