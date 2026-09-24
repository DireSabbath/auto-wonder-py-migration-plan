"""恢复打包时，检查点仓库基线排在交付修订前面。这些检查不连接 MySQL。"""

import logging
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace

import pytest

from autowonder.artifacts.models import Artifact
from autowonder.dispatch.checkpoint import RevisionArtifact
from autowonder.dispatch.models import Dispatch
from autowonder.taskpackages.assembler import _checkpoint_revision, _source_revisions
from tests.unit.test_workitems import MemorySession

_REVISION = "deliverables/runtime-source-revision.json"


class _Rows(MemorySession):
    async def get(self, model: type[object], key: int) -> object | None:
        for row in self.rows:
            if isinstance(row, model) and getattr(row, "id", None) == key:
                return row
        return None

    async def run_sync(self, fn: Callable[[object], object]) -> object:
        return fn(object())


def _dispatch(
    dispatch_id: int,
    status: str,
    delivery_source: int | None,
    idempotency_key: str,
) -> Dispatch:
    moment = datetime(2026, 9, 24, 8, 0, 0)
    return Dispatch(
        id=dispatch_id,
        tenant_id=1,
        source_type="WORKITEM",
        workitem_id=77,
        agent_id=30,
        status=status,
        attempt=1,
        idempotency_key=idempotency_key,
        delivery_source_dispatch_id=delivery_source,
        version=1,
        is_deleted=0,
        gmt_create=moment,
        gmt_modified=moment,
    )


def _artifact(dispatch_id: int, name: str, oss_ref: str) -> Artifact:
    return Artifact(
        id=dispatch_id,
        tenant_id=1,
        source_type="WORKITEM",
        workitem_id=77,
        dispatch_id=dispatch_id,
        name=name,
        type="FILE",
        oss_ref=oss_ref,
    )


async def _load(rows: list[object]) -> _Rows:
    session = _Rows()
    for row in rows:
        session.add(row)
    await session.flush()
    return session


def test_checkpoint_revision_asks_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """装配把同步会话交给已有的检查点引擎，不另写归档解析。"""
    seen: dict[str, object] = {}

    class Engine:
        def __init__(self, storage: object, bucket: str) -> None:
            seen["storage"] = storage
            seen["bucket"] = bucket

        def find_repo_revision(
            self, tenant_id: int, dispatch_id: int, repo: object
        ) -> RevisionArtifact:
            seen["tenant"] = tenant_id
            seen["dispatch"] = dispatch_id
            seen["repo"] = repo
            return RevisionArtifact(
                "checkpoint/3/deliverables/runtime-source-revision.json",
                "bucket/side",
            )

    monkeypatch.setattr("autowonder.taskpackages.assembler.CheckpointEngine", Engine)
    monkeypatch.setattr(
        "autowonder.taskpackages.assembler.SqlCheckpointRepo",
        lambda session: ("repo", session),
    )
    monkeypatch.setattr("autowonder.taskpackages.assembler.get_object_storage", lambda: "storage")
    monkeypatch.setattr(
        "autowonder.taskpackages.assembler.resolve_bucket", lambda workload, fallback: "bucket"
    )
    monkeypatch.setattr(
        "autowonder.taskpackages.assembler.get_settings",
        lambda: SimpleNamespace(oss_task_pkg_bucket="task", oss_bucket="fallback"),
    )
    sync_session = object()
    found = _checkpoint_revision(sync_session, 7, 20)
    assert found is not None
    assert found.oss_ref == "bucket/side"
    assert seen["storage"] == "storage"
    assert seen["bucket"] == "bucket"
    assert seen["tenant"] == 7
    assert seen["dispatch"] == 20
    assert seen["repo"] == ("repo", sync_session)


async def test_recovery_puts_checkpoint_baselines_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """沿来源链先收集各派发的检查点基线，再接它们的交付修订。"""
    found_ids: list[int] = []

    def lookup(sync_session: object, tenant_id: int, dispatch_id: int) -> RevisionArtifact:
        found_ids.append(dispatch_id)
        return RevisionArtifact(
            "checkpoint/" + str(dispatch_id) + "/" + _REVISION,
            "oss/checkpoint/" + str(dispatch_id),
        )

    monkeypatch.setattr("autowonder.taskpackages.assembler._checkpoint_revision", lookup)
    session = await _load(
        [
            _dispatch(20, "FAILED", 10, "continue:10"),
            _dispatch(10, "FAILED", None, "handoff:99"),
            _artifact(20, "artifacts/output/" + _REVISION, "oss/delivery/20"),
            _artifact(20, "handoff/summary.md", "oss/summary"),
            _artifact(10, "deliverables\\runtime-source-revision.json", "oss/delivery/10"),
        ]
    )
    refs = await _source_revisions(session, 1, 77, 99, 20, True)
    assert found_ids == [20, 10]
    assert [(ref.name, ref.oss_ref) for ref in refs] == [
        ("checkpoint/20/" + _REVISION, "oss/checkpoint/20"),
        ("checkpoint/10/" + _REVISION, "oss/checkpoint/10"),
        ("artifacts/output/" + _REVISION, "oss/delivery/20"),
        ("deliverables\\runtime-source-revision.json", "oss/delivery/10"),
    ]


async def test_checkpoint_failure_keeps_delivery_revisions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """检查点查询失败只记警告，已收集的交付修订仍然返回。"""

    def lookup(sync_session: object, tenant_id: int, dispatch_id: int) -> RevisionArtifact:
        raise RuntimeError("storage down")

    monkeypatch.setattr("autowonder.taskpackages.assembler._checkpoint_revision", lookup)
    session = await _load(
        [
            _dispatch(20, "FAILED", None, "continue:1"),
            _artifact(20, _REVISION, "oss/delivery/20"),
        ]
    )
    with caplog.at_level(logging.WARNING):
        refs = await _source_revisions(session, 1, 77, 99, 20, True)
    assert [(ref.name, ref.oss_ref) for ref in refs] == [(_REVISION, "oss/delivery/20")]
    assert "checkpoint repo revision fallback unavailable sourceDispatchId=20" in caplog.text


async def test_succeeded_delivery_skips_checkpoint_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非恢复来源只收成功派发的修订，不查检查点。"""
    calls: list[int] = []

    def lookup(sync_session: object, tenant_id: int, dispatch_id: int) -> RevisionArtifact:
        calls.append(dispatch_id)
        return RevisionArtifact("checkpoint/1/" + _REVISION, "oss/checkpoint/1")

    monkeypatch.setattr("autowonder.taskpackages.assembler._checkpoint_revision", lookup)
    session = await _load(
        [
            _dispatch(20, "SUCCEEDED", None, "handoff:1"),
            _artifact(20, _REVISION, "oss/delivery/20"),
        ]
    )
    refs = await _source_revisions(session, 1, 77, 99, 20, False)
    assert calls == []
    assert [(ref.name, ref.oss_ref) for ref in refs] == [(_REVISION, "oss/delivery/20")]
