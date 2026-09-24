"""AI 场景校验和确认落库。"""

import json
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.ai.models import AiSession
from autowonder.clarifications.models import Clarification
from autowonder.memories.models import Memory
from autowonder.repos.models import Repo, RepoConclusion
from autowonder.sdlcs.models import Sdlc, SdlcStep

logger = logging.getLogger(__name__)

SCENES = ("REPO_SCAN", "MEMORY_IMPORT", "SDLC_GEN", "AGENT_CONFIG_GEN", "CLARIFICATION")


def validate_result(scene: str, result_json: str | None) -> str | None:
    """按场景检查确认结果。通过时返回 None。"""
    if scene == "REPO_SCAN":
        return _repo_scan(result_json)
    if scene == "MEMORY_IMPORT":
        return _memory(result_json)
    if scene == "SDLC_GEN":
        return _sdlc(result_json)
    if scene == "AGENT_CONFIG_GEN":
        return _agent(result_json)
    if scene == "CLARIFICATION":
        return _clarification(result_json)
    return None


async def persist_confirmed(
    session: AsyncSession,
    ai_session: AiSession,
    result_json: str | None,
) -> None:
    """把已通过校验的结果写进对应业务表。"""
    payload = _object(result_json)
    if payload is None:
        return
    if ai_session.scene == "REPO_SCAN":
        await _persist_repo(session, ai_session, payload)
    elif ai_session.scene == "MEMORY_IMPORT":
        await _persist_memory(session, ai_session, payload)
    elif ai_session.scene == "CLARIFICATION":
        await _persist_clarification(session, ai_session, payload)
    elif ai_session.scene == "SDLC_GEN":
        await _persist_sdlc(session, ai_session, payload)


def _repo_scan(result_json: str | None) -> str | None:
    length = 0 if result_json is None else len(result_json)
    logger.info("repo scan validateResult jsonLen=%s", length)
    try:
        obj = _object(result_json)
        purpose = None if obj is None else obj.get("purpose")
        if obj is None or not isinstance(purpose, str) or purpose.strip() == "":
            return "missing or blank purpose field"
        if obj.get("summaryMd") is None:
            return "missing summaryMd field"
        return None
    except Exception as error:
        return "invalid JSON: " + str(error)


def _memory(result_json: str | None) -> str | None:
    try:
        obj = _object(result_json)
        if obj is None:
            return "null result"
        items = obj.get("items")
        if not isinstance(items, list) or len(items) == 0:
            return "items array is empty"
        for index, item in enumerate(items):
            title = item.get("title") if isinstance(item, dict) else None
            if not isinstance(title, str) or title.strip() == "":
                return "item[" + str(index) + "] missing title"
        return None
    except Exception as error:
        return "invalid JSON: " + str(error)


def _sdlc(result_json: str | None) -> str | None:
    try:
        obj = _object(_extract_object(result_json))
        if obj is None:
            return "null result"
        steps = obj.get("steps")
        if not isinstance(steps, list) or len(steps) == 0:
            return "steps array is empty"
        for index, step in enumerate(steps):
            missing_name = not isinstance(step, dict) or step.get("name") is None
            missing_instruction = not isinstance(step, dict) or step.get("instructionMd") is None
            if missing_name or missing_instruction:
                return "step " + str(index) + " missing name or instructionMd"
        return None
    except Exception as error:
        return "invalid JSON: " + str(error)


def _agent(result_json: str | None) -> str | None:
    try:
        obj = _object(_extract_object(result_json))
        if obj is None:
            return "null result"
        for key in ("name", "roleName", "roleCode", "businessBackground", "responsibilities"):
            value = obj.get(key)
            if value is not None and not isinstance(value, str):
                return key + " must be a string"
        if (
            "missingFields" in obj
            and obj.get("missingFields") is not None
            and not isinstance(obj.get("missingFields"), list)
        ):
            return "missingFields must be an array"
        questions = obj.get("clarifyingQuestions")
        questions_present = "clarifyingQuestions" in obj and questions is not None
        if questions_present and not isinstance(questions, list):
            return "clarifyingQuestions must be an array"
        recommendations = obj.get("recommendations")
        if isinstance(recommendations, dict):
            for key in ("executors", "skills", "memories", "workflows"):
                value = recommendations.get(key)
                if value is not None and not isinstance(value, list):
                    return "recommendations." + key + " must be an array"
        elif recommendations is not None:
            return "recommendations must be an object"
        return None
    except Exception as error:
        return "invalid JSON: " + str(error)


def _clarification(result_json: str | None) -> str | None:
    try:
        obj = _object(result_json)
        text = None if obj is None else obj.get("clarificationMd")
        if obj is None or not isinstance(text, str) or text.strip() == "":
            return "missing clarificationMd field"
        return None
    except Exception as error:
        return "invalid JSON: " + str(error)


async def _persist_repo(
    session: AsyncSession,
    ai_session: AiSession,
    payload: dict[str, object],
) -> None:
    if ai_session.biz_ref_id is None:
        return
    repo = await session.get(Repo, ai_session.biz_ref_id)
    if repo is None or repo.tenant_id != ai_session.tenant_id:
        logger.warning("repo not found for scan confirm repoId=%s", ai_session.biz_ref_id)
        return
    existing = await session.scalar(
        select(RepoConclusion)
        .where(
            RepoConclusion.tenant_id == ai_session.tenant_id,
            RepoConclusion.repo_id == ai_session.biz_ref_id,
        )
        .limit(1)
    )
    if existing is None:
        session.add(
            RepoConclusion(
                tenant_id=ai_session.tenant_id,
                repo_id=ai_session.biz_ref_id,
                purpose=_text(payload.get("purpose")),
                key_business=payload.get("keyBusiness"),
                upstreams=payload.get("upstreams"),
                downstreams=payload.get("downstreams"),
                summary_md=_text(payload.get("summaryMd")),
                ai_session_id=ai_session.id,
                version=0,
            )
        )
    else:
        existing.purpose = _text(payload.get("purpose"))
        existing.key_business = payload.get("keyBusiness")
        existing.upstreams = payload.get("upstreams")
        existing.downstreams = payload.get("downstreams")
        existing.summary_md = _text(payload.get("summaryMd"))
        existing.version = existing.version + 1
    await session.flush()
    await session.execute(
        update(Repo)
        .where(
            Repo.id == repo.id,
            Repo.tenant_id == ai_session.tenant_id,
            Repo.version == repo.version,
        )
        .values(scan_status="CONCLUDED", version=Repo.version + 1)
    )


async def _persist_memory(
    session: AsyncSession,
    ai_session: AiSession,
    payload: dict[str, object],
) -> None:
    items = payload.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        session.add(
            Memory(
                tenant_id=ai_session.tenant_id,
                scope="ORG",
                owner_ref=ai_session.biz_ref_id,
                type=_text(item.get("type")),
                title=_text(item.get("title")) or "",
                content_md=_text(item.get("contentMd")),
                status="PENDING",
                source="AI_IMPORT",
                source_ref={"aiSessionId": ai_session.id},
            )
        )
    await session.flush()


async def _persist_clarification(
    session: AsyncSession,
    ai_session: AiSession,
    payload: dict[str, object],
) -> None:
    if ai_session.biz_ref_id is None:
        return
    content = _text(payload.get("clarificationMd"))
    existing = await session.scalar(
        select(Clarification)
        .where(
            Clarification.tenant_id == ai_session.tenant_id,
            Clarification.workitem_id == ai_session.biz_ref_id,
        )
        .limit(1)
    )
    if existing is None:
        session.add(
            Clarification(
                tenant_id=ai_session.tenant_id,
                workitem_id=ai_session.biz_ref_id,
                content_md=content,
            )
        )
    else:
        existing.content_md = content
        existing.version = existing.version + 1
    await session.flush()


async def _persist_sdlc(
    session: AsyncSession,
    ai_session: AiSession,
    payload: dict[str, object],
) -> None:
    name = _text(payload.get("name"))
    if name is None or name.strip() == "":
        name = "AI Generated SDLC"
    sdlc = Sdlc(
        tenant_id=ai_session.tenant_id,
        name=name,
        description=_text(payload.get("description")),
        status="DRAFT",
        is_default=0,
        version=0,
    )
    session.add(sdlc)
    await session.flush()
    steps = payload.get("steps")
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, dict):
            continue
        order = step.get("order")
        step_order = 0
        if isinstance(order, int) and not isinstance(order, bool):
            step_order = order
        required_flag = 1
        if step.get("required") is False:
            required_flag = 0
        session.add(
            SdlcStep(
                tenant_id=ai_session.tenant_id,
                sdlc_id=sdlc.id,
                step_order=step_order,
                name=_text(step.get("name")) or "",
                kind=_text(step.get("kind")),
                instruction_md=_text(step.get("instructionMd")),
                checklist_json=step.get("checklist"),
                gate_policy_json=step.get("gatePolicy"),
                required=required_flag,
                timeout_seconds=_int_or_none(step.get("timeoutSeconds")),
                retry_budget=_int_or_none(step.get("retryBudget")),
            )
        )
    await session.flush()


def _object(result_json: str | None) -> dict[str, object] | None:
    if result_json is None:
        return None
    parsed = json.loads(result_json)
    if not isinstance(parsed, dict):
        return None
    return parsed


def _extract_object(result_json: str | None) -> str | None:
    if result_json is None:
        return None
    text = result_json.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return result_json


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None
