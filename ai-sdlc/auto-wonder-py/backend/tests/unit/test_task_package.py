"""任务包布局、永久失败分类，以及拿不到派发锁时直接返回。"""

import zipfile
from datetime import UTC, datetime
from io import BytesIO
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.pending import (
    permanent_package_input_failure,
    root_failure_message,
    run_pending,
)
from autowonder.storage.objects import InMemoryObjectStorage
from autowonder.taskpackages.context import PackageContext
from autowonder.taskpackages.packager import TaskPackager


def _clock() -> datetime:
    return datetime(2026, 9, 24, 1, 2, 3, tzinfo=UTC)


def test_build_writes_generated_skill_and_manifest() -> None:
    """没有归档的技能会生成 SKILL.md，清单里的包号是 ``pkg_{dispatch}``。"""
    storage = InMemoryObjectStorage()
    packager = TaskPackager(storage, "task-pkg", "https://example.test/api/mcp/", _clock)
    context = PackageContext(
        tenant_id=7,
        workitem_id=8,
        dispatch_id=9,
        workitem_title="标题",
        workitem_content_md="正文",
        skills=[{"type": "SKILL", "name": "review", "id": "11", "description": "看代码"}],
    )
    built = packager.build(context)
    assert built.oss_ref == "task-pkg/7/8/9.zip"
    assert built.download_url == "mem://task-pkg/7/8/9.zip?ttl=600"
    assert built.sha256 == built.content_hash
    assert not built.sha256.startswith("sha256:")
    stored = storage.get(built.oss_ref)
    assert stored is not None
    archive = zipfile.ZipFile(BytesIO(stored))
    names = archive.namelist()
    assert "workitem.md" in names
    assert "identity.json" in names
    assert "repos.json" in names
    assert "policy.json" in names
    assert "skills.json" in names
    assert "sdlc.json" in names
    assert "manifest.json" in names
    assert "capabilities/skills/review/SKILL.md" in names
    assert archive.read("workitem.md") == "标题\n\n正文".encode()
    skill = archive.read("capabilities/skills/review/SKILL.md").decode()
    assert 'name: "review"' in skill
    assert "# review" in skill
    assert "看代码" in skill
    manifest = archive.read("manifest.json").decode()
    assert '"packageId":"pkg_9"' in manifest
    assert '"createdAt":"2026-09-24T01:02:03Z"' in manifest
    assert "manifest.json" not in manifest.split('"fileDigests":')[1].split("}")[0]


def test_duplicate_capability_is_a_permanent_package_failure() -> None:
    """重复的类型加名称中断打包，原因链上的 ValueError 不再重试。"""
    storage = InMemoryObjectStorage()
    packager = TaskPackager(storage, "task-pkg", "https://example.test/api/mcp", _clock)
    context = PackageContext(
        tenant_id=1,
        workitem_id=2,
        dispatch_id=3,
        skills=[
            {"type": "SKILL", "name": "review", "id": "1"},
            {"type": "SKILL", "name": "review", "id": "2"},
        ],
    )
    with pytest.raises(BizError) as caught:
        packager.build(context)
    assert caught.value.error_code == ErrorCode.PACKAGE_BUILD_FAILED
    assert permanent_package_input_failure(caught.value)
    assert "duplicate capability SKILL:review" in root_failure_message(caught.value)


def test_permanent_input_recognizes_state_and_capability_errors() -> None:
    """30005、参数错误和明确的能力配置错误是永久失败。"""
    assert permanent_package_input_failure(
        BizError(ErrorCode.SCHEDULED_TASK_INVALID_STATE, "snapshot")
    )
    assert permanent_package_input_failure(
        RuntimeError("bound capability is missing or belongs to another tenant: 4")
    )
    assert permanent_package_input_failure(
        RuntimeError("capability config must be a JSON object: 4")
    )
    assert permanent_package_input_failure(RuntimeError("COMMENT_REWORK_CONTEXT_MISSING: gone"))
    assert not permanent_package_input_failure(RuntimeError("storage timeout"))


async def test_run_pending_returns_false_when_the_dispatch_lock_is_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁被占用时不读派发，直接返回假。"""

    async def busy(_lock_key: str, _owner: str, _ttl: int) -> bool:
        return False

    monkeypatch.setattr("autowonder.dispatch.pending.try_acquire_lock", busy)
    assert await run_pending(cast(AsyncSession, object()), 9) is False
