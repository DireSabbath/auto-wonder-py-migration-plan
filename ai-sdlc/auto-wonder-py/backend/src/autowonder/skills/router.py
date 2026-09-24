"""``/api/skills``。查看要求只读，改技能和打标要求读写。"""

import json
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.api.access import WorkspaceAccessLevel, require_access
from autowonder.categories.service import batch_set_skill_category, set_skill_category
from autowonder.core.context import current_user_id, current_workspace_id
from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import fail_omitting_nulls, ok
from autowonder.db.session import get_session
from autowonder.skills.package import (
    FORMAT_TAR_GZ,
    create_from_package,
    inspect_package,
    list_package_files,
    load_package,
    read_package_file,
    skill_bucket,
    update_package,
)
from autowonder.skills.schemas import (
    CreateSkillRequest,
    UpdateSkillRequest,
    category_id_from_json,
    skill_ids_from_json,
)
from autowonder.skills.service import (
    create_skill,
    delete_skill,
    get_skill,
    list_skills,
    update_skill,
)
from autowonder.storage.objects import get_object_storage

router = APIRouter(
    prefix="/api/skills",
    tags=["skills"],
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_ONLY, "查看技能"))],
)


def _user_id() -> int:
    user_id = current_user_id()
    if user_id is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    return user_id


def _workspace_id() -> int:
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise BizError(ErrorCode.WORKSPACE_NOT_MEMBER)
    return workspace_id


async def _json_body(request: Request) -> object:
    raw = await request.body()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise BizError(ErrorCode.PARAM_INVALID) from error


@router.post(
    "",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "创建技能"))],
)
async def create(
    body: CreateSkillRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按安装规格创建技能。"""
    return ok(await create_skill(session, body, _workspace_id(), _user_id()))


@router.get("")
async def list_page(
    skill_type: Annotated[str | None, Query(alias="type")] = None,
    category_id: Annotated[int | None, Query(alias="categoryId")] = None,
    include_descendants: Annotated[bool, Query(alias="includeDescendants")] = True,
    uncategorized: Annotated[bool, Query(alias="uncategorized")] = False,
    page: Annotated[int, Query()] = 1,
    size: Annotated[int, Query()] = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """分页列出技能。"""
    return ok(
        await list_skills(
            session,
            _workspace_id(),
            skill_type,
            category_id,
            include_descendants,
            uncategorized,
            page,
            size,
        )
    )


@router.post(
    "/category/batch",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "批量设置能力分类"))],
)
async def batch_category(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """批量打标。categoryId 为 null 时取消打标。"""
    body = await _json_body(request)
    skill_ids = skill_ids_from_json(body)
    category_id = category_id_from_json(body)
    return ok(
        await batch_set_skill_category(
            session,
            skill_ids,
            category_id,
            _workspace_id(),
            _user_id(),
        )
    )


@router.post("/package/inspect")
async def inspect(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """读取技能包根上的 SKILL.md，不写入技能。"""
    return ok(inspect_package(file.filename, await file.read()))


@router.post(
    "/package",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "从技能包创建技能"))],
)
async def create_package(
    file: Annotated[UploadFile, File()],
    skill_type: Annotated[str, Query(alias="type")] = "SKILL",
    name: Annotated[str | None, Query()] = None,
    description: Annotated[str | None, Query()] = None,
    providers: Annotated[list[str] | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """从上传的技能包、插件包或 Hook 包创建技能。"""
    return ok(
        await create_from_package(
            session,
            get_object_storage(),
            skill_bucket(),
            file.filename,
            await file.read(),
            skill_type,
            name,
            description,
            providers,
            _workspace_id(),
            _user_id(),
        )
    )


@router.get("/{id}")
async def get(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """技能详情。"""
    return ok(await get_skill(session, id))


@router.put(
    "/{id}/category",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "设置能力分类"))],
)
async def set_category(
    id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """设置或取消能力主分类。"""
    category_id = category_id_from_json(await _json_body(request))
    return ok(await set_skill_category(session, id, category_id, _workspace_id(), _user_id()))


@router.get("/{id}/package/files")
async def package_files(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """列出技能包里的文件和补出来的目录。"""
    skill = await get_skill(session, id)
    return ok(list_package_files(get_object_storage(), skill))


@router.get("/{id}/package/file")
async def package_file(
    id: int,
    path: Annotated[str, Query()],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """读取技能包里的一个文本文件。"""
    skill = await get_skill(session, id)
    return ok(read_package_file(get_object_storage(), skill, path))


@router.get("/{id}/package/download")
async def download_package(id: int, session: AsyncSession = Depends(get_session)) -> Response:
    """按原格式下载技能包。业务失败改成带正确类型的 JSON。"""
    try:
        skill = await get_skill(session, id)
        download = load_package(get_object_storage(), skill)
    except BizError as error:
        return _download_error(error)
    if download.format == FORMAT_TAR_GZ:
        media_type = "application/gzip"
    else:
        media_type = "application/zip"
    return Response(
        content=download.data,
        media_type=media_type,
        headers={
            "Content-Disposition": _attachment(download.file_name),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put(
    "/{id}/package",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新技能包"))],
)
async def replace_package(
    id: int,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Query()] = None,
    description: Annotated[str | None, Query()] = None,
    providers: Annotated[list[str] | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """用新的技能包替换已有技能，类型保持不变。"""
    return ok(
        await update_package(
            session,
            get_object_storage(),
            skill_bucket(),
            id,
            file.filename,
            await file.read(),
            name,
            description,
            providers,
            _workspace_id(),
            _user_id(),
        )
    )


@router.put(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "更新技能"))],
)
async def update(
    id: int,
    body: UpdateSkillRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按版本号更新技能。null 字段保留原值。"""
    return ok(await update_skill(session, id, body, _workspace_id(), _user_id()))


@router.delete(
    "/{id}",
    dependencies=[Depends(require_access(WorkspaceAccessLevel.READ_WRITE, "删除技能"))],
)
async def delete(id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """软删除技能。仍被引用时拒绝。"""
    await delete_skill(session, id, _workspace_id(), _user_id())
    return ok(None)


def _download_error(error: BizError) -> Response:
    if error.code == ErrorCode.SKILL_NOT_FOUND.code:
        status_code = 404
    else:
        status_code = 400
    body = json.dumps(
        fail_omitting_nulls(error.error_code, str(error)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return Response(
        content=body,
        status_code=status_code,
        media_type="application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _attachment(filename: str) -> str:
    escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
    encoded = quote(filename, safe="")
    return 'attachment; filename="' + escaped + "\"; filename*=UTF-8''" + encoded
