"""项目配置备份的允许表、打包和路径。这些检查不访问数据库。"""

import hashlib
import io
import json
import zipfile

from fastapi.testclient import TestClient

from autowonder.backups.archive import (
    BUCKET_MESSAGE,
    MAX_BYTES,
    SIZE_LIMIT_MESSAGE,
    Snapshot,
    build_archive,
    failure_message,
    page_bounds,
    require_download,
    select_backup_bucket,
)
from autowonder.backups.rules import RULES
from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app
from autowonder.storage.objects import MapObjectStorage


def test_backup_rules_keep_the_java_allowlist() -> None:
    """只导出 Java 点名的表，密文值和飞书凭据列不在名单里。"""
    tables = [rule.table for rule in RULES]
    assert "workitem" not in tables
    assert "dispatch" not in tables
    assert "audit_log" not in tables
    assert "squad_template" in tables
    org = next(rule for rule in RULES if rule.table == "org")
    assert org.predicate == "`id` = :workspace_id AND is_deleted = 0"
    shared = next(rule for rule in RULES if rule.table == "squad_template")
    assert "tenant_id IS NULL" in shared.predicate
    scheduled = next(rule for rule in RULES if rule.table == "scheduled_task")
    assert scheduled.predicate.startswith("`workspace_id` = :workspace_id")
    setting = next(rule for rule in RULES if rule.table == "system_setting")
    assert "CASE WHEN is_secret = 1 THEN NULL ELSE value_json END AS value_json" in setting.columns
    feishu = next(rule for rule in RULES if rule.table == "feishu_robot_binding")
    assert "credential_ref" not in feishu.columns
    agent = next(rule for rule in RULES if rule.table == "agent")
    assert "`kind`" not in agent.columns
    assert "ORDER BY id" in org.sql()


def test_bucket_page_and_download_guards() -> None:
    """桶回退、分页上限和未成功备份的下载拒绝与 Java 一致。"""
    assert select_backup_bucket("", "artifacts", "base") == "artifacts"
    assert select_backup_bucket("", "", "base") == "base"
    assert select_backup_bucket("backup", "artifacts", "base") == "backup"
    assert select_backup_bucket(" ", " ", "legacy") == "legacy"
    assert page_bounds(-1, 999) == (1, 100)
    assert page_bounds(2, 0) == (2, 1)
    assert BUCKET_MESSAGE.startswith("请先配置持久化对象存储")
    try:
        require_download("FAILED", "bucket/a.zip", True)
    except BizError as error:
        assert error.error_code == ErrorCode.CONFLICT
        assert str(error) == "备份尚未成功，无法下载"
    else:
        raise AssertionError("expected unfinished backup")
    try:
        require_download("SUCCEEDED", "bucket/a.zip", False)
    except BizError as error:
        assert error.error_code == ErrorCode.NOT_FOUND
        assert str(error) == "备份文件已不存在"
    else:
        raise AssertionError("expected missing object")
    assert require_download("SUCCEEDED", "bucket/a.zip", True) == "bucket/a.zip"
    assert failure_message(RuntimeError("sensitive provider detail")) == (
        "备份失败，请检查数据库、对象存储配置及技能包文件后重试"
    )
    assert "sensitive" not in failure_message(RuntimeError("sensitive provider detail"))
    assert failure_message(BizError(ErrorCode.PARAM_INVALID, SIZE_LIMIT_MESSAGE)) == (
        SIZE_LIMIT_MESSAGE
    )


def test_archive_includes_skill_package_and_checksums() -> None:
    """技能包按 MD5 纳入压缩包，清单记录排除项和校验和。"""
    storage = MapObjectStorage()
    skill = b"skill contents"
    stored = storage.put("skills", "1.zip", skill)
    snapshot = Snapshot(
        files={
            "config/memory.json": json.dumps(
                [{"id": 1, "content_md": "retained knowledge"}],
                ensure_ascii=False,
            ).encode(),
            "config/agent_repo_perm.json": json.dumps(
                [{"id": 1, "allowed_branch_patterns": '["release/*"]'}],
            ).encode(),
        },
        counts={"memory": 1, "agent_repo_perm": 1, "skill": 1},
        skills=[
            {
                "id": 1,
                "package_oss_ref": stored.oss_ref,
                "package_md5": stored.md5,
                "package_size": len(skill),
            }
        ],
        captured_at="2026-09-23T12:00:00Z",
    )
    archive = build_archive(snapshot, "backup-1", 1, 9, storage)
    files = _unzip(archive)
    manifest = json.loads(files["manifest.json"])
    assert manifest["format"] == "autowonder-project-config"
    assert manifest["formatVersion"] == 1
    assert manifest["workspaceId"] == "1"
    assert manifest["createdBy"] == "9"
    assert files["packages/skill-1.zip"] == skill
    assert "config/workitem.json" not in files
    memory = json.loads(files["config/memory.json"])
    assert memory[0]["content_md"] == "retained knowledge"
    patterns = json.loads(files["config/agent_repo_perm.json"])
    assert patterns[0]["allowed_branch_patterns"] == '["release/*"]'
    for path, digest in manifest["sha256"].items():
        assert digest == hashlib.sha256(files[path]).hexdigest()
    assert manifest["skillPackages"] == [
        {
            "skillId": "1",
            "originalOssRef": stored.oss_ref,
            "path": "packages/skill-1.zip",
        }
    ]


def test_bad_skill_packages_stop_before_a_bad_upload() -> None:
    """缺失、校验失败或声明体积过大时不读取或中止打包。"""
    missing = MapObjectStorage()
    snapshot = Snapshot(
        files={},
        counts={},
        skills=[{"id": 7, "package_oss_ref": "skills/missing.zip", "package_size": 1}],
        captured_at="t",
    )
    try:
        build_archive(snapshot, "id", 1, 9, missing)
    except BizError as error:
        assert "技能包文件缺失" in str(error)
        assert "7" in str(error)
    else:
        raise AssertionError("expected missing skill package")
    corrupt = MapObjectStorage()
    corrupt.put("skills", "corrupt.zip", b"\x01")
    bad = Snapshot(
        files={},
        counts={},
        skills=[
            {
                "id": 8,
                "package_oss_ref": "skills/corrupt.zip",
                "package_md5": "invalid",
                "package_size": 1,
            }
        ],
        captured_at="t",
    )
    try:
        build_archive(bad, "id", 1, 9, corrupt)
    except BizError as error:
        assert error.error_code == ErrorCode.CONFLICT
        assert "技能包校验失败" in str(error)
    else:
        raise AssertionError("expected corrupt skill package")
    huge = MapObjectStorage()
    oversized = Snapshot(
        files={},
        counts={},
        skills=[
            {
                "id": 9,
                "package_oss_ref": "skills/large.zip",
                "package_size": MAX_BYTES + 1,
            }
        ],
        captured_at="t",
    )
    try:
        build_archive(oversized, "id", 1, 9, huge)
    except BizError as error:
        assert str(error) == SIZE_LIMIT_MESSAGE
    else:
        raise AssertionError("expected oversized package")
    assert huge.gets == []


def test_backup_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/workspaces/current/backups" in paths
    assert "/api/workspaces/current/backups/{id}/download" in paths
    assert "post" in paths["/api/workspaces/current/backups"]
    assert "get" in paths["/api/workspaces/current/backups"]
    response = client.get("/api/workspaces/current/backups")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"


def _unzip(payload: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            files[name] = archive.read(name)
    return files
