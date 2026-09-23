"""从产物里的 ``observability/trace.json`` 读取完成态轨迹。"""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.artifacts.models import Artifact
from autowonder.core.errors import BizError, ErrorCode, IllegalArgumentError
from autowonder.dispatch.schemas import RuntimeObservation, RuntimeTrace, RuntimeTurn
from autowonder.storage.objects import get_object_storage

TRACE_NAME = "observability/trace.json"
_CONTEXT_PREFIX = "context/files/"


@dataclass(frozen=True)
class ContextContent:
    """上下文文件的引用和原始字节。"""

    content_ref: str
    payload: bytes


def artifacts_for_dispatch(tenant_id: int, dispatch_id: int) -> Select[tuple[Artifact]]:
    """同一派发的全部产物，新的在前。观测文件也包含在内。"""
    return (
        select(Artifact)
        .where(Artifact.tenant_id == tenant_id, Artifact.dispatch_id == dispatch_id)
        .order_by(Artifact.id.desc())
    )


def name_matches(name: str | None, logical_name: str) -> bool:
    """名称相等，或以 ``/`` 加逻辑名结尾。"""
    if name == logical_name:
        return True
    if name is None:
        return False
    return name.endswith("/" + logical_name)


def find_named_artifact(rows: list[Artifact], logical_name: str) -> Artifact | None:
    """按列表顺序找第一个匹配的产物。"""
    for row in rows:
        if name_matches(row.name, logical_name):
            return row
    return None


def require_named_artifact(rows: list[Artifact], logical_name: str) -> Artifact:
    """找不到时按产物不存在处理。"""
    found = find_named_artifact(rows, logical_name)
    if found is None:
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    return found


def trace_from_bytes(payload: bytes | None, dispatch_id: int) -> RuntimeTrace:
    """解析轨迹 JSON。空对象视为产物不存在，坏的调度 id 退回路径 id。"""
    if payload is None:
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    document = json.loads(payload.decode("utf-8"))
    if document is None:
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    if not isinstance(document, dict):
        raise RuntimeError("trace document is not a JSON object")
    return trace_from_document(document, dispatch_id)


def trace_from_document(document: dict[str, Any], dispatch_id: int) -> RuntimeTrace:
    """把轨迹文档收成模型，并按 Java 再解析一次调度 id。"""
    raw_id = document.get("dispatchId")
    body = dict(document)
    if "dispatchId" in body:
        del body["dispatchId"]
    trace = RuntimeTrace.model_validate(body)
    trace.dispatch_id = _coerce_dispatch_id(raw_id, dispatch_id)
    return trace


def outline_from_bytes(payload: bytes | None, dispatch_id: int) -> RuntimeTrace:
    """完成态大纲来源标成 OSS，并去掉提示词和观测载荷。"""
    trace = trace_from_bytes(payload, dispatch_id)
    trace.source = "OSS"
    strip_outline(trace)
    return trace


def strip_outline(trace: RuntimeTrace) -> None:
    """大纲不带回提示词、回合输出和观测载荷。"""
    for session in trace.sessions:
        for turn in session.turns:
            turn.prompt = None
            turn.system_prompt = None
            turn.output = None
            strip_observations(turn.observations)


def strip_observations(observations: list[RuntimeObservation] | None) -> None:
    """递归清掉观测的输入、输出和错误。"""
    if observations is None:
        return
    for observation in observations:
        observation.input = None
        observation.output = None
        observation.error = None
        strip_observations(observation.children)


def find_turn(trace: RuntimeTrace, trace_id: str) -> RuntimeTurn:
    """按 traceId 或 turnId 找回合。"""
    for session in trace.sessions:
        for turn in session.turns:
            if trace_id == turn.trace_id or trace_id == turn.turn_id:
                return turn
    raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)


def find_observation(
    observations: list[RuntimeObservation] | None,
    observation_id: str,
) -> RuntimeObservation | None:
    """在观测树里按 id 查找。"""
    if observations is None:
        return None
    for observation in observations:
        if observation_id == observation.observation_id:
            return observation
        child = find_observation(observation.children, observation_id)
        if child is not None:
            return child
    return None


def require_observation(trace: RuntimeTrace, observation_id: str) -> RuntimeObservation:
    """找不到观测时按产物不存在处理。"""
    for session in trace.sessions:
        for turn in session.turns:
            found = find_observation(turn.observations, observation_id)
            if found is not None:
                return found
    raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)


def validate_content_ref(content_ref: str) -> None:
    """上下文引用必须落在 ``context/files/`` 下，且不能穿越。"""
    invalid = False
    if not content_ref.startswith(_CONTEXT_PREFIX):
        invalid = True
    if content_ref.startswith("/"):
        invalid = True
    if "\\" in content_ref:
        invalid = True
    if ".." in content_ref:
        invalid = True
    if invalid:
        raise IllegalArgumentError("invalid context content ref")


async def load_outline_if_present(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> RuntimeTrace | None:
    """没有轨迹产物时返回空，调用方再去投影事件。"""
    rows = list(await session.scalars(artifacts_for_dispatch(tenant_id, dispatch_id)))
    found = find_named_artifact(rows, TRACE_NAME)
    if found is None:
        return None
    payload = get_object_storage().get(found.oss_ref)
    return outline_from_bytes(payload, dispatch_id)


async def load_turn(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    trace_id: str,
) -> RuntimeTurn:
    """读取完整回合，保留提示词。"""
    trace = await _load_trace(session, tenant_id, dispatch_id)
    return find_turn(trace, trace_id)


async def load_observation(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    observation_id: str,
) -> RuntimeObservation:
    """读取一条观测，保留输入和输出。"""
    trace = await _load_trace(session, tenant_id, dispatch_id)
    return require_observation(trace, observation_id)


async def load_context(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
    content_ref: str,
) -> ContextContent:
    """按校验过的引用读取上下文原文。"""
    validate_content_ref(content_ref)
    rows = list(await session.scalars(artifacts_for_dispatch(tenant_id, dispatch_id)))
    found = require_named_artifact(rows, "observability/" + content_ref)
    payload = get_object_storage().get(found.oss_ref)
    if payload is None:
        raise BizError(ErrorCode.ARTIFACT_NOT_FOUND)
    return ContextContent(content_ref, payload)


async def _load_trace(
    session: AsyncSession,
    tenant_id: int,
    dispatch_id: int,
) -> RuntimeTrace:
    rows = list(await session.scalars(artifacts_for_dispatch(tenant_id, dispatch_id)))
    found = require_named_artifact(rows, TRACE_NAME)
    payload = get_object_storage().get(found.oss_ref)
    return trace_from_bytes(payload, dispatch_id)


def _coerce_dispatch_id(raw: object, fallback: int) -> int:
    if raw is None:
        rendered = "null"
    else:
        rendered = str(raw)
    try:
        return int(rendered)
    except ValueError:
        return fallback
