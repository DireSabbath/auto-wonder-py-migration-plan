"""平台管理员名册、候选人和一次性初始化标记。这些检查不访问数据库。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data
from autowonder.main import create_app
from autowonder.platform.admins import (
    LAST_ADMIN_DENIED,
    SELF_REMOVAL_DENIED,
    add_platform_admin,
    candidate_statement,
    ensure_platform_admin_initialized,
    list_admins_statement,
    mark_admin_statement,
    mark_init_done_statement,
    normalize_admin_keyword,
    remove_platform_admin,
    revoke_admin_statement,
    roster_view,
)
from autowonder.users.models import User


def test_roster_marks_self_and_keeps_other_admins_removable() -> None:
    """两名管理员时，自己不可移除，另一名可以。"""
    roster = roster_view(
        True,
        10000,
        [_user(10000, "alice", 1, 0), _user(10001, "bob", 1, 0)],
    )
    assert roster.can_manage is True
    body = dump_data(roster)
    assert body["canManage"] is True
    self_row = body["admins"][0]
    assert self_row["self"] is True
    assert self_row["removable"] is False
    assert self_row["removeDisabledReason"] == SELF_REMOVAL_DENIED
    other = body["admins"][1]
    assert other["username"] == "bob"
    assert other["active"] is True
    assert other["self"] is False
    assert other["removable"] is True
    assert other["removeDisabledReason"] is None


def test_last_admin_cannot_be_removed_even_by_a_viewer() -> None:
    """只剩一名时按钮不可用，原因是至少保留一名。旁观者也不能管理。"""
    roster = roster_view(False, 10000, [_user(10001, "bob", 1, 0)])
    only = dump_data(roster)["admins"][0]
    assert roster.can_manage is False
    assert only["self"] is False
    assert only["removable"] is False
    assert only["removeDisabledReason"] == LAST_ADMIN_DENIED


def test_deactivated_admin_stays_on_the_roster_as_inactive() -> None:
    """注销后的管理员仍占名额，active 为假。"""
    roster = roster_view(False, 10002, [_user(10001, "bob", 1, 1)])
    assert dump_data(roster)["admins"][0]["active"] is False


def test_keyword_trim_matches_java_and_keeps_nbsp() -> None:
    """空关键字与缺省相同。不换行空格不会被 trim 掉。"""
    assert normalize_admin_keyword("  carol  ") == "carol"
    assert normalize_admin_keyword(None) == ""
    assert normalize_admin_keyword("\u00a0carol\u00a0") == "\u00a0carol\u00a0"


def test_admin_sql_matches_the_roster_and_candidate_rules() -> None:
    """名册按 id 升序。空关键字不加 LIKE。提升和撤销都带 is_admin 条件。"""
    listed = _sql(list_admins_statement())
    assert "is_deleted = 0" in listed
    assert "is_admin = 1" in listed
    assert "ORDER BY" in listed
    assert listed.endswith("id ASC") or "id ASC" in listed
    empty = _sql(candidate_statement("", 20))
    assert "LIKE" not in empty
    assert "status = 0" in empty
    assert "is_admin = 0" in empty
    assert empty.endswith("LIMIT 20")
    matched = _sql(candidate_statement("carol", 20))
    assert "LIKE" in matched
    assert "%carol%" in matched
    promoted = _sql(mark_admin_statement(10002))
    assert "is_admin=0" in promoted.replace(" ", "") or "is_admin = 0" in promoted
    revoked = _sql(revoke_admin_statement(10001))
    assert "is_admin = 1" in revoked
    marker = _sql(mark_init_done_statement())
    assert "platform_admin_init" in marker
    assert "ON DUPLICATE KEY UPDATE" in marker


async def test_add_refuses_a_non_admin_before_reading_the_target() -> None:
    """授权失败时不读取目标用户，也不写提升语句。"""
    session = _Seq([_user(10002, "carol", 0, 0), _user(10003, "dave", 0, 0)])
    with pytest.raises(BizError, match="仅平台管理员可以添加平台管理员") as caught:
        await add_platform_admin(session, 10002, 10003)
    assert caught.value.code == ErrorCode.NO_PERMISSION.code
    assert len(session.users) == 1
    assert session.executed == []


async def test_add_rejects_missing_and_inactive_targets() -> None:
    """没有用户、找不到、已停用都不提升。"""

    async def allow(_session: object, _user_id: int | None, _action: str) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("autowonder.platform.admins.require_system_admin", allow)
    try:
        missing = _Seq([])
        with pytest.raises(BizError) as caught:
            await add_platform_admin(missing, 10000, None)
        assert caught.value.code == ErrorCode.SYSTEM_ADMIN_USER_REQUIRED.code
        absent = _Seq([None])
        with pytest.raises(BizError) as caught:
            await add_platform_admin(absent, 10000, 10003)
        assert caught.value.code == ErrorCode.SYSTEM_ADMIN_TARGET_NOT_FOUND.code
        inactive = _Seq([_user(10004, "dave", 0, 1)])
        with pytest.raises(BizError) as caught:
            await add_platform_admin(inactive, 10000, 10004)
        assert caught.value.code == ErrorCode.SYSTEM_ADMIN_TARGET_NOT_FOUND.code
        assert inactive.executed == []
    finally:
        monkeypatch.undo()


async def test_add_promotes_an_active_user() -> None:
    """活跃用户会被提升，语句带 is_admin = 0。"""

    async def allow(_session: object, _user_id: int | None, _action: str) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("autowonder.platform.admins.require_system_admin", allow)
    try:
        session = _Seq([_user(10002, "carol", 0, 0)])
        await add_platform_admin(session, 10000, 10002)
    finally:
        monkeypatch.undo()
    assert len(session.executed) == 1
    assert "is_admin" in session.executed[0]


async def test_remove_refuses_self_and_the_last_admin() -> None:
    """先拒绝移除自己；名册只剩一人时拒绝清空。"""

    async def allow(_session: object, _user_id: int | None, _action: str) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("autowonder.platform.admins.require_system_admin", allow)
    try:
        session = _Seq([])
        with pytest.raises(BizError, match=SELF_REMOVAL_DENIED):
            await remove_platform_admin(session, 10000, 10000)
        assert session.executed == []
        last = _Seq([_user(10001, "bob", 1, 0)], counts=[1])
        with pytest.raises(BizError, match=LAST_ADMIN_DENIED):
            await remove_platform_admin(last, 10000, 10001)
        assert all("update" not in sql for sql in last.executed)
    finally:
        monkeypatch.undo()


async def test_remove_demotes_another_admin() -> None:
    """多于一名时撤销另一名管理员。"""

    async def allow(_session: object, _user_id: int | None, _action: str) -> None:
        return None

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("autowonder.platform.admins.require_system_admin", allow)
    try:
        session = _Seq([_user(10001, "bob", 1, 0)], counts=[2])
        await remove_platform_admin(session, 10000, 10001)
    finally:
        monkeypatch.undo()
    assert session.executed[-1].startswith("update")
    assert "is_admin" in session.executed[-1]


async def test_init_marker_skips_promotion_and_records_completion() -> None:
    """标记已完成时不再查询名册。尚无标记且已有管理员时只写标记。"""
    done = _Seq([], counts=[1])
    assert await ensure_platform_admin_initialized(done) is False
    assert done.executed == []
    pending = _Seq([], counts=[0, 1])
    assert await ensure_platform_admin_initialized(pending) is False
    assert any("platform_admin_init" in sql for sql in pending.executed)


def test_platform_admin_routes_require_login() -> None:
    """名册、候选人和删除都要登录，候选人路径不与用户 id 冲突。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "get" in paths["/api/platform/admins"]
    assert "post" in paths["/api/platform/admins"]
    assert "get" in paths["/api/platform/admins/candidates"]
    assert "delete" in paths["/api/platform/admins/{userId}"]
    listed = client.get("/api/platform/admins")
    candidates = client.get("/api/platform/admins/candidates")
    removed = client.delete("/api/platform/admins/10001")
    assert listed.status_code == 401
    assert listed.json()["code"] == "10401"
    assert candidates.status_code == 401
    assert removed.status_code == 401


class _Count:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _Seq:
    """按调用顺序交回用户或计数。计数语句走 execute，其余用户查询走 scalar。"""

    def __init__(self, users: list[User | None], counts: list[int] | None = None) -> None:
        self.users = list(users)
        self.counts = list(counts or [])
        self.executed: list[str] = []

    async def scalar(self, statement: object) -> User | int | None:
        sql = str(statement).lower()
        if "count" in sql:
            return self.counts.pop(0)
        return self.users.pop(0)

    async def execute(self, statement: object) -> _Count:
        sql = str(statement).lower()
        self.executed.append(sql)
        if "count" in sql:
            return _Count(self.counts.pop(0))
        return _Count(1)


def _user(user_id: int, username: str, is_admin: int, status: int) -> User:
    return User(
        id=user_id,
        username=username,
        nickname=username,
        email=username + "@example.com",
        status=status,
        is_admin=is_admin,
        password_hash="hash",
    )


def _sql(statement: object) -> str:
    compiled = statement.compile(  # type: ignore[attr-defined]
        dialect=mysql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)
