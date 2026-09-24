"""执行器申请调试日志直传地址。这条路径在鉴权白名单里，靠执行器令牌校验。"""

import json
import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.artifacts.daemon_auth import DetailedUploadAuth, authenticate_detailed
from autowonder.db.session import get_session
from autowonder.debuglogs.issue import expires_text, issue_upload
from autowonder.debuglogs.sanitizer import SHA256_HEX, java_is_blank
from autowonder.dispatch.models import Dispatch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/daemon", tags=["daemon-debug-logs"])

_REPORTABLE = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELED"})


@router.post("/dispatches/{dispatchId}/debug-log-upload")
async def request_upload(
    dispatchId: int,
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """按 404、403、409、422、400 的顺序决定能否签发。失败统一返回 issue_failed。"""
    body = await _body(request)
    auth = await authenticate_detailed(session, dispatchId, token)
    rejected = _reject(auth, body)
    if rejected is not None:
        return rejected
    dispatch = cast(Dispatch, auth.dispatch)
    payload = cast(dict[str, Any], body)
    try:
        issued = await issue_upload(
            session,
            dispatch,
            _size(payload),
            _sha256(payload),
            _truncated(payload),
            cast(str, payload.get("dispatchStatus")),
        )
    except Exception as error:
        logger.error(
            "debug log issue failed dispatchId=%s error=%s",
            dispatchId,
            type(error).__name__ + ": " + str(error),
            exc_info=True,
        )
        return JSONResponse(status_code=503, content={"error": "issue_failed"})
    logger.info(
        "debug log upload issued dispatchId=%s objectKey=%s alreadyUploaded=%s",
        dispatchId,
        issued.object_key,
        issued.already_uploaded,
    )
    expires_at = expires_text(issued.expires_at)
    return JSONResponse(
        status_code=200,
        content={
            "objectKey": issued.object_key,
            "uploadUrl": issued.upload_url,
            "expiresAt": expires_at,
            "alreadyUploaded": issued.already_uploaded,
        },
    )


def _reject(auth: DetailedUploadAuth, body: dict[str, Any] | None) -> JSONResponse | None:
    if auth.status == "DISPATCH_NOT_FOUND":
        return JSONResponse(status_code=404, content={"error": "dispatch_not_found"})
    if auth.status != "OK":
        return JSONResponse(status_code=403, content={"error": "token_invalid"})
    dispatch = cast(Dispatch, auth.dispatch)
    if dispatch.status not in _REPORTABLE:
        return JSONResponse(status_code=409, content={"error": "dispatch_not_terminal"})
    if dispatch.debug_log_enabled != 1:
        return JSONResponse(status_code=422, content={"error": "debug_log_disabled"})
    if body is None:
        return JSONResponse(status_code=400, content={"error": "invalid_dispatch_status"})
    if body.get("dispatchStatus") not in _REPORTABLE:
        return JSONResponse(status_code=400, content={"error": "invalid_dispatch_status"})
    size_bytes = body.get("sizeBytes")
    if isinstance(size_bytes, int) and not isinstance(size_bytes, bool) and size_bytes < 0:
        return JSONResponse(status_code=400, content={"error": "invalid_size_bytes"})
    sha256 = body.get("sha256")
    if isinstance(sha256, str) and not java_is_blank(sha256):
        if SHA256_HEX.fullmatch(sha256) is None:
            return JSONResponse(status_code=400, content={"error": "invalid_sha256"})
    return None


async def _body(request: Request) -> dict[str, Any] | None:
    raw = await request.body()
    if raw == b"":
        return None
    loaded = json.loads(raw)
    if isinstance(loaded, dict):
        return loaded
    return None


def _size(body: dict[str, Any] | None) -> int | None:
    if body is None:
        return None
    size_bytes = body.get("sizeBytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        return None
    return size_bytes


def _sha256(body: dict[str, Any] | None) -> str | None:
    if body is None:
        return None
    sha256 = body.get("sha256")
    if not isinstance(sha256, str) or java_is_blank(sha256):
        return None
    return sha256


def _truncated(body: dict[str, Any] | None) -> bool:
    if body is None:
        return False
    return body.get("truncated") is True
