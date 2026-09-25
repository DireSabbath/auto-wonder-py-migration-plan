"""Aone 入站轮询的增量窗口、评论导入和对账游标。"""

import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import unquote_plus

import pytest
from sqlalchemy import delete, select

from autowonder.config import get_settings
from autowonder.db.session import SessionLocal, engine
from autowonder.integrations import aone_api
from autowonder.integrations.aone_api import (
    AoneClient,
    AoneConfig,
    ExternalPrincipalRef,
    ExternalWorkitemDetail,
    to_detail,
)
from autowonder.integrations.aone_codec import AoneOpenApiError
from autowonder.integrations.aone_schemas import AoneSyncResult
from autowonder.integrations.aone_sync import (
    _create,
    _import_comments,
    _load_comments,
    incremental_from,
    reconcile_cursor,
    reconcile_linked_workitems,
    sync_binding_increment,
    sync_issue_ids,
)
from autowonder.integrations.models import (
    ExternalCommentLink,
    ExternalPrincipal,
    ExternalProjectBinding,
    ExternalWorkitemLink,
)
from autowonder.notifications.models import Notification
from autowonder.security.crypto import AesGcmSecretCrypto
from autowonder.workitems.models import Workitem, WorkitemComment, WorkitemEvent
from autowonder.workitems.service import get_workitem, list_workitems
from autowonder.workitems.timeline import unified_timeline

_SECRET = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_TENANT = 880000241
_STAFF = "staff-9"


def test_issue_detail_keeps_source_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """优先级、未知类型、深链和参与者按 Aone 映射。同一工号只留第一次。"""
    monkeypatch.setenv("AUTOWONDER_AONE_WEB_BASE_URL", "https://aone.example/")
    detail = to_detail(
        {
            "id": 77001,
            "akProjectId": 2161074,
            "stamp": "Req",
            "subject": "身份快照",
            "priorityId": "94",
            "priority": "Urgent",
            "userStaffId": "staff-rep",
            "userName": "提出人",
            "assignedToStaffId": "staff-own",
            "assignedTo": "负责人",
            "participants": [
                {"staffId": "staff-p", "name": "参与"},
                {"id": "staff-p", "name": "重复"},
                " ",
            ],
            "watchers": ["staff-w"],
        }
    )
    assert detail.work_type == "REQ"
    assert detail.priority == 0
    assert detail.external_url == "https://aone.example/v2/project/2161074/req/77001"
    assert detail.reporter is not None
    assert detail.reporter.subject_id == "staff-rep"
    assert detail.reporter.display_name == "提出人"
    assert detail.business_owner is not None
    assert detail.business_owner.subject_id == "staff-own"
    assert len(detail.principal_relations) == 2
    assert detail.principal_relations[0].principals[0].subject_id == "staff-p"
    assert detail.principal_relations[0].principals[0].display_name == "参与"
    assert len(detail.principal_relations[0].principals) == 1
    assert detail.principal_relations[1].principals[0].subject_id == "staff-w"
    assert detail.principal_relations[1].principals[0].display_name is None
    assert to_detail({"id": "1", "stamp": "Epic"}).work_type == "TASK"
    assert to_detail({"id": "2"}).priority == 2
    assert to_detail({"id": "3", "priority": "High"}).priority == 1
    assert to_detail({"id": "4", "priorityId": "97"}).priority == 3
    assert to_detail({"id": "5", "priorityId": "96", "priority": "Medium"}).priority == 2
    bug = to_detail({"id": "6", "akProjectId": "9", "stamp": "Bug"})
    assert bug.external_url == "https://aone.example/v2/project/9/bug/6"


def test_incremental_window_and_reconcile_cursor() -> None:
    """首次没有下界；之后重叠 1 小时。坏游标从 0 再扫。"""
    last = datetime(2026, 9, 23, 12, 0, 0)
    assert incremental_from(None) is None
    assert incremental_from(last) == datetime(2026, 9, 23, 11, 0, 0)
    assert reconcile_cursor(None) == 0
    assert reconcile_cursor("  ") == 0
    assert reconcile_cursor("12") == 12
    assert reconcile_cursor("-3") == 0
    assert reconcile_cursor("nope") == 0


@pytest.fixture
def aone_on(monkeypatch: pytest.MonkeyPatch):
    """打开 Aone，并去掉客户端限速，结束后恢复配置缓存。"""
    monkeypatch.setenv("AUTOWONDER_AONE_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(aone_api._LIMITER, "acquire", lambda: None)
    yield
    get_settings.cache_clear()


@pytest.fixture
async def release_engine():
    yield
    await engine.dispose()


class _State:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail_batch = False
        self.comments: list[dict[str, object]] = []
        self.issues: list[dict[str, object]] = []
        self.user: dict[str, object] = {"staffId": _STAFF, "userName": "阿一"}


class _Handler(BaseHTTPRequestHandler):
    state = _State()

    def do_GET(self) -> None:
        _Handler.state.calls.append(("GET", self.path, ""))
        if "UserApiFacade/getById" in self.path:
            self._send({"success": True, "result": _Handler.state.user})
            return
        if "CommentTopService/get" in self.path:
            if _Handler.state.fail_batch and "%2C" in self.path:
                self._send({"success": False, "message": "invoke exception,null"})
                return
            matched = [
                item
                for item in _Handler.state.comments
                if "%5B" + str(item["targetId"]) + "%5D" in self.path
                or "%5B" + str(item["targetId"]) + "%2C" in self.path
            ]
            if not _Handler.state.fail_batch:
                matched = list(_Handler.state.comments)
            self._send({"success": True, "result": matched})
            return
        self._send({"success": True, "result": []})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode()
        _Handler.state.calls.append(("POST", self.path, raw))
        if "searchV4" in self.path and len(_Handler.state.issues) > 0:
            self._send(
                {
                    "success": True,
                    "result": _Handler.state.issues,
                    "totalCount": len(_Handler.state.issues),
                }
            )
            return
        self._send({"success": True, "result": [], "totalCount": 0})

    def _send(self, payload: dict[str, object]) -> None:
        import json

        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _Server:
    def __init__(self, state: _State) -> None:
        _Handler.state = state
        self.state = state
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        self.base = "http://" + str(host) + ":" + str(port)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def _config(base: str) -> AoneConfig:
    return AoneConfig(base, "auto-wonder", _SECRET, "1")


def test_comment_batch_failure_retries_each_issue(aone_on: None) -> None:
    """20 条一批失败后逐条再拉，只留下成功的那条。"""
    state = _State()
    state.fail_batch = True
    state.comments = [
        {
            "id": "9",
            "targetId": "100",
            "content": "from aone",
            "userStaffId": "s1",
        }
    ]
    server = _Server(state)
    try:
        comments = _load_comments(
            AoneClient(),
            _config(server.base),
            SimpleNamespace(id=1),
            [str(100 + index) for index in range(20)],
        )
    finally:
        server.stop()
    assert len(comments) == 1
    assert comments[0].content_md == "from aone"
    assert comments[0].author_staff_id == "s1"
    gets = [path for method, path, _body in state.calls if method == "GET"]
    assert len(gets) == 21


async def test_empty_poll_uses_overlap_and_resets_cursor(
    aone_on: None,
    release_engine: None,
) -> None:
    """失败不改上次成功；空结果记下成功，并把走到头的游标拨回 0。"""
    state = _State()
    server = _Server(state)
    cipher = AesGcmSecretCrypto(_SECRET)
    binding_id = None
    previous = datetime(2026, 9, 23, 12, 0, 0)
    try:
        async with SessionLocal() as session:
            binding = ExternalProjectBinding(
                tenant_id=_TENANT,
                provider="AONE",
                external_project_id="2161074",
                external_project_name="inbound",
                base_url="http://127.0.0.1:1",
                client_key="auto-wonder",
                credential_ref=cipher.encrypt(_SECRET),
                region_id="1",
                poll_interval_seconds=15,
                enabled=1,
                last_success_at=previous,
                reconcile_cursor="9",
                creator_id=9,
                is_deleted=0,
            )
            session.add(binding)
            await session.flush()
            binding_id = binding.id
            with pytest.raises(AoneOpenApiError):
                await sync_binding_increment(session, AoneClient(), cipher, binding, 9)
            assert binding.last_success_at == previous
            binding.base_url = server.base
            synced = await sync_binding_increment(session, AoneClient(), cipher, binding, 9)
            assert synced == 0
            assert binding.last_error is None
            assert binding.last_success_at != previous
            reconciled = await reconcile_linked_workitems(
                session,
                AoneClient(),
                cipher,
                binding,
                9,
                100,
            )
            assert reconciled == 0
            assert binding.reconcile_cursor == "0"
            await session.commit()
        posted = [body for method, _path, body in state.calls if method == "POST"]
        assert "createdAtFrom=2026-09-23 11:00:00" in unquote_plus(posted[0])
    finally:
        server.stop()
        await _purge(binding_id, None)


async def test_fake_aone_comment_becomes_inbound_comment(
    aone_on: None,
    release_engine: None,
) -> None:
    """假 Aone 的评论写成 INBOUND，删除改文案，写回回声不再导入。"""
    state = _State()
    state.comments = [
        {
            "id": 88011,
            "targetId": 501,
            "content": "inbound-live-body",
            "userId": 42,
            "createdAt": 1720680000000,
            "updatedAt": 1720680300000,
        }
    ]
    server = _Server(state)
    cipher = AesGcmSecretCrypto(_SECRET)
    binding_id = None
    workitem_id = None
    try:
        async with SessionLocal() as session:
            binding = ExternalProjectBinding(
                tenant_id=_TENANT,
                provider="AONE",
                external_project_id="2161074",
                external_project_name="inbound",
                base_url=server.base,
                client_key="auto-wonder",
                credential_ref=cipher.encrypt(_SECRET),
                region_id="1",
                enabled=1,
                creator_id=9,
                is_deleted=0,
            )
            workitem = Workitem(
                tenant_id=_TENANT,
                work_type="TASK",
                title="入站评论",
                assignee_type="HUMAN",
                assignee_ref=42,
                creator_id=7,
            )
            session.add(binding)
            session.add(workitem)
            await session.flush()
            binding_id = binding.id
            workitem_id = workitem.id
            session.add(
                ExternalWorkitemLink(
                    tenant_id=_TENANT,
                    provider="AONE",
                    binding_id=binding.id,
                    external_project_id="2161074",
                    external_workitem_id="501",
                    workitem_id=workitem.id,
                )
            )
            await session.flush()
            client = AoneClient()
            config = _config(server.base)
            created = AoneSyncResult()
            await _import_comments(session, client, config, binding, ["501"], created, 9)
            assert created.comments_imported == 1
            local = await session.scalar(
                select(WorkitemComment).where(WorkitemComment.workitem_id == workitem.id)
            )
            assert local is not None
            assert local.author_type == "EXTERNAL"
            assert local.content_md == "inbound-live-body"
            principal = await session.scalar(
                select(ExternalPrincipal).where(
                    ExternalPrincipal.provider == "AONE",
                    ExternalPrincipal.subject_id == _STAFF,
                )
            )
            assert principal is not None
            assert principal.display_name == "阿一"
            assert local.author_ref == principal.id
            link = await session.scalar(
                select(ExternalCommentLink).where(
                    ExternalCommentLink.binding_id == binding.id,
                    ExternalCommentLink.external_comment_id == "88011",
                )
            )
            assert link is not None
            assert link.direction == "INBOUND"
            assert link.source_status == "ACTIVE"
            notice = await session.scalar(
                select(Notification).where(
                    Notification.tenant_id == _TENANT,
                    Notification.ref_id == workitem.id,
                )
            )
            assert notice is not None
            assert notice.type == "EXTERNAL_COMMENT"
            assert notice.recipient_id == 42
            assert notice.title == "外部工单有新回复"
            assert notice.content == "阿一：inbound-live-body"
            assert notice.link == "/workitems/" + str(workitem.id)

            state.comments = [
                {
                    "id": 88011,
                    "targetId": 501,
                    "content": "旧正文",
                    "userStaffId": _STAFF,
                    "userName": "阿一",
                    "createdAt": 1720680000000,
                    "updatedAt": 1720680400000,
                    "isDeleted": True,
                }
            ]
            deleted = AoneSyncResult()
            await _import_comments(session, client, config, binding, ["501"], deleted, 9)
            assert deleted.comments_imported == 1
            await session.refresh(local)
            await session.refresh(link)
            assert local.content_md == "（该外部评论已在来源平台删除）"
            assert link.source_status == "DELETED"
            event = await session.scalar(
                select(WorkitemEvent).where(
                    WorkitemEvent.workitem_id == workitem.id,
                    WorkitemEvent.event_type == "EXTERNAL_COMMENT_DELETE",
                )
            )
            assert event is not None
            assert event.from_val == "88011"

            session.add(
                ExternalCommentLink(
                    tenant_id=_TENANT,
                    provider="AONE",
                    binding_id=binding.id,
                    external_workitem_id="501",
                    external_comment_id="88012",
                    workitem_comment_id=local.id,
                    direction="OUTBOUND",
                    source_status="ACTIVE",
                )
            )
            await session.flush()
            state.comments = [
                {
                    "id": 88012,
                    "targetId": 501,
                    "content": "本地写回的评论",
                    "userStaffId": _STAFF,
                    "updatedAt": 1720680500000,
                }
            ]
            echo = AoneSyncResult()
            await _import_comments(session, client, config, binding, ["501"], echo, 9)
            assert echo.comments_imported == 0
            comments = list(
                await session.scalars(
                    select(WorkitemComment).where(WorkitemComment.workitem_id == workitem.id)
                )
            )
            assert len(comments) == 1
            await session.commit()
    finally:
        server.stop()
        await _purge(binding_id, workitem_id)


_IDENTITY_TENANT = 880000251
_RACE_TENANT = 880000252
_IDENTITY_SUBJECTS = ("staff-rep", "staff-own", "staff-own-2", "staff-p", "staff-w")


def _identity_issue(
    owner_staff: str, owner_name: str, updated_at: int, closed: bool
) -> dict[str, object]:
    return {
        "id": 77001,
        "akProjectId": 2161074,
        "stamp": "Req",
        "subject": "身份快照" if owner_staff == "staff-own" else "身份快照-更新",
        "description": "正文",
        "status": "待处理",
        "statusId": 1,
        "priorityId": "94",
        "userStaffId": "staff-rep",
        "userName": "提出人",
        "assignedToStaffId": owner_staff,
        "assignedTo": owner_name,
        "participants": [{"staffId": "staff-p", "name": "参与"}],
        "watchers": ["staff-w"],
        "createdAt": 1720680000000,
        "updatedAt": updated_at,
        "isClosed": closed,
    }


async def test_synced_issue_exposes_source_identity(
    aone_on: None,
    release_engine: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """假 Aone 工单的提出者和负责人出现在详情、列表和时间线上。"""
    monkeypatch.setenv("AUTOWONDER_AONE_WEB_BASE_URL", "https://aone.example/")
    state = _State()
    state.issues = [_identity_issue("staff-own", "负责人", 1720680300000, False)]
    server = _Server(state)
    cipher = AesGcmSecretCrypto(_SECRET)
    binding_id = None
    try:
        async with SessionLocal() as session:
            binding = ExternalProjectBinding(
                tenant_id=_IDENTITY_TENANT,
                provider="AONE",
                external_project_id="2161074",
                external_project_name="identity",
                base_url=server.base,
                client_key="auto-wonder",
                credential_ref=cipher.encrypt(_SECRET),
                region_id="1",
                enabled=1,
                creator_id=9,
                is_deleted=0,
            )
            session.add(binding)
            await session.flush()
            binding_id = binding.id
            await sync_issue_ids(session, AoneClient(), cipher, binding, ["77001"], 9)
            link = await session.scalar(
                select(ExternalWorkitemLink).where(ExternalWorkitemLink.binding_id == binding.id)
            )
            assert link is not None
            view = await get_workitem(session, link.workitem_id, _IDENTITY_TENANT, 9)
            collaboration = view.external_collaboration
            assert collaboration is not None
            assert collaboration.provider == "AONE"
            assert collaboration.external_workitem_id == "77001"
            assert (
                collaboration.external_url == "https://aone.example/v2/project/2161074/req/77001"
            )
            assert collaboration.reporter is not None
            assert collaboration.reporter.subject_id == "staff-rep"
            assert collaboration.reporter.display_name == "提出人"
            assert collaboration.business_owner is not None
            assert collaboration.business_owner.subject_id == "staff-own"
            assert collaboration.last_error is None
            assert collaboration.source_lifecycle == "ACTIVE"
            assert len(collaboration.principal_relations) == 2
            assert collaboration.principal_relations[0].source_key == "participants"
            assert collaboration.principal_relations[0].display_name == "参与者"
            assert collaboration.principal_relations[0].principals[0].subject_id == "staff-p"
            assert collaboration.principal_relations[1].source_key == "watchers"
            assert collaboration.principal_relations[1].principals[0].display_name is None
            assert view.priority == 0
            assert view.work_type == "REQ"
            assert view.source_creator is None
            page = await list_workitems(
                session,
                _IDENTITY_TENANT,
                9,
                None,
                None,
                None,
                None,
                None,
                False,
                None,
                None,
                None,
                None,
                1,
                20,
            )
            assert page.total == 1
            card = page.list_[0]
            assert card.source_creator is not None
            assert card.source_creator.subject_id == "staff-rep"
            assert card.external_collaboration is None
            assert card.source_provider == "AONE"
            items = await unified_timeline(session, link.workitem_id)
            imported = [item for item in items if item.content == "已从 Aone 工单导入"]
            assert len(imported) == 1
            assert imported[0].source_provider == "AONE"
            assert imported[0].source_external_workitem_id == "77001"
            assert imported[0].source_external_url == collaboration.external_url
            link.last_error = "旧错误"
            link.last_error_code = "OLD"
            await session.flush()
            state.issues = [_identity_issue("staff-own-2", "新负责人", 1720680400000, True)]
            await sync_issue_ids(session, AoneClient(), cipher, binding, ["77001"], 9)
            await session.refresh(link)
            assert link.last_error is None
            assert link.last_error_code is None
            assert link.source_lifecycle == "CLOSED"
            assert link.business_owner_principal_id != collaboration.business_owner.id
            owner_event = await session.scalar(
                select(WorkitemEvent).where(
                    WorkitemEvent.workitem_id == link.workitem_id,
                    WorkitemEvent.event_type == "EXTERNAL_BUSINESS_OWNER_CHANGE",
                )
            )
            assert owner_event is not None
            assert owner_event.from_val == str(collaboration.business_owner.id)
            assert owner_event.to_val == str(link.business_owner_principal_id)
            lifecycle_event = await session.scalar(
                select(WorkitemEvent).where(
                    WorkitemEvent.workitem_id == link.workitem_id,
                    WorkitemEvent.event_type == "EXTERNAL_LIFECYCLE_CHANGE",
                )
            )
            assert lifecycle_event is not None
            assert lifecycle_event.from_val == "ACTIVE"
            assert lifecycle_event.to_val == "CLOSED"
            changed = await unified_timeline(session, link.workitem_id)
            owner_lines = [
                item.content
                for item in changed
                if item.content is not None and item.content.startswith("外部业务负责人已变更")
            ]
            assert owner_lines == [
                "外部业务负责人已变更: 负责人（staff-own） → 新负责人（staff-own-2）"
            ]
            lifecycle_lines = [
                item.content
                for item in changed
                if item.content is not None and item.content.startswith("来源工单生命周期已变更")
            ]
            assert lifecycle_lines == ["来源工单生命周期已变更: ACTIVE → CLOSED"]
            updated = [item for item in changed if item.content == "已从 Aone 工单同步更新"]
            assert len(updated) == 1
            assert updated[0].source_external_workitem_id == "77001"
            await session.commit()
    finally:
        server.stop()
        await _purge_tenant(binding_id, _IDENTITY_TENANT, _IDENTITY_SUBJECTS)


async def test_raced_link_soft_deletes_the_new_workitem(
    aone_on: None,
    release_engine: None,
) -> None:
    """链接唯一键冲突时丢掉刚建的工单，并改写已经存在的那条。"""
    cipher = AesGcmSecretCrypto(_SECRET)
    binding_id = None
    try:
        async with SessionLocal() as session:
            binding = ExternalProjectBinding(
                tenant_id=_RACE_TENANT,
                provider="AONE",
                external_project_id="2161074",
                external_project_name="race",
                base_url="http://127.0.0.1:9",
                client_key="auto-wonder",
                credential_ref=cipher.encrypt(_SECRET),
                region_id="1",
                enabled=1,
                creator_id=9,
                is_deleted=0,
            )
            kept = Workitem(
                tenant_id=_RACE_TENANT,
                work_type="TASK",
                title="已存在",
                assignee_type="EXTERNAL",
                assignee_ref=0,
                creator_id=9,
                version=0,
            )
            session.add(binding)
            session.add(kept)
            await session.flush()
            binding_id = binding.id
            session.add(
                ExternalWorkitemLink(
                    tenant_id=_RACE_TENANT,
                    provider="AONE",
                    binding_id=binding.id,
                    external_project_id="2161074",
                    external_workitem_id="77002",
                    external_work_type="TASK",
                    workitem_id=kept.id,
                    source_lifecycle="ACTIVE",
                    last_error="旧错误",
                    last_error_code="OLD",
                    sync_status="ACTION_REQUIRED",
                )
            )
            await session.flush()
            detail = ExternalWorkitemDetail(
                external_id="77002",
                external_project_id="2161074",
                work_type="TASK",
                title="race",
                status_name="待处理",
                status_id="1",
                priority=2,
                source_lifecycle="ACTIVE",
                reporter=ExternalPrincipalRef("staff-race", "竞态提出人"),
                raw_json="{}",
            )
            await _create(session, binding, detail, "digest", 9)
            orphans = list(
                await session.scalars(
                    select(Workitem).where(
                        Workitem.tenant_id == _RACE_TENANT,
                        Workitem.is_deleted == 1,
                    )
                )
            )
            assert len(orphans) == 1
            assert orphans[0].id != kept.id
            assert orphans[0].version == 1
            assert orphans[0].modifier_id == 9
            link = await session.scalar(
                select(ExternalWorkitemLink).where(ExternalWorkitemLink.binding_id == binding.id)
            )
            assert link is not None
            assert link.workitem_id == kept.id
            assert link.last_error is None
            assert link.last_error_code is None
            reporter = await session.scalar(
                select(ExternalPrincipal).where(
                    ExternalPrincipal.provider == "AONE",
                    ExternalPrincipal.subject_id == "staff-race",
                )
            )
            assert reporter is not None
            assert link.reporter_principal_id == reporter.id
            await session.commit()
    finally:
        await _purge_tenant(binding_id, _RACE_TENANT, ("staff-race",))


async def _purge_tenant(binding_id: int | None, tenant_id: int, subjects: tuple[str, ...]) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(WorkitemEvent).where(WorkitemEvent.tenant_id == tenant_id))
        if binding_id is not None:
            await session.execute(
                delete(ExternalWorkitemLink).where(ExternalWorkitemLink.binding_id == binding_id)
            )
            await session.execute(
                delete(ExternalProjectBinding).where(ExternalProjectBinding.id == binding_id)
            )
        await session.execute(delete(Workitem).where(Workitem.tenant_id == tenant_id))
        if len(subjects) > 0:
            await session.execute(
                delete(ExternalPrincipal).where(
                    ExternalPrincipal.provider == "AONE",
                    ExternalPrincipal.subject_id.in_(subjects),
                )
            )
        await session.commit()


async def _purge(binding_id: int | None, workitem_id: int | None) -> None:
    async with SessionLocal() as session:
        if workitem_id is not None:
            await session.execute(
                delete(Notification).where(
                    Notification.tenant_id == _TENANT,
                    Notification.ref_id == workitem_id,
                )
            )
            await session.execute(
                delete(WorkitemEvent).where(
                    WorkitemEvent.tenant_id == _TENANT,
                    WorkitemEvent.workitem_id == workitem_id,
                )
            )
        if binding_id is not None:
            await session.execute(
                delete(ExternalCommentLink).where(ExternalCommentLink.binding_id == binding_id)
            )
            await session.execute(
                delete(ExternalWorkitemLink).where(ExternalWorkitemLink.binding_id == binding_id)
            )
        if workitem_id is not None:
            await session.execute(
                delete(WorkitemComment).where(
                    WorkitemComment.tenant_id == _TENANT,
                    WorkitemComment.workitem_id == workitem_id,
                )
            )
            await session.execute(delete(Workitem).where(Workitem.id == workitem_id))
        if binding_id is not None:
            await session.execute(
                delete(ExternalProjectBinding).where(ExternalProjectBinding.id == binding_id)
            )
        await session.execute(
            delete(ExternalPrincipal).where(
                ExternalPrincipal.provider == "AONE",
                ExternalPrincipal.subject_id == _STAFF,
            )
        )
        await session.commit()
