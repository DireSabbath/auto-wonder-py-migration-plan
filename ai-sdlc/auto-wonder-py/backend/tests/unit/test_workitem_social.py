"""工单评论、关注和评论指引。这些检查不连接 MySQL。"""

from datetime import datetime

from autowonder.agents.models import Agent, AgentVersion
from autowonder.core.errors import BizError, ErrorCode
from autowonder.dispatch.enqueue import enqueue_comment_interaction, enqueue_workitem
from autowonder.dispatch.models import Dispatch, DispatchRecoveryCheckpoint
from autowonder.guidance.mentions import (
    mention_comparable_content,
    mention_names,
    text_mention_index,
)
from autowonder.guidance.service import attach_interaction_statuses, create_for_comment
from autowonder.notifications.models import WorkitemCommentDelivery
from autowonder.sdlcs.models import SdlcStep
from autowonder.users.models import User
from autowonder.workitems.comments import add_comment
from autowonder.workitems.models import (
    Workitem,
    WorkitemComment,
    WorkitemCommentMention,
    WorkitemEvent,
    WorkitemWatcher,
)
from autowonder.workitems.schemas import ParticipantView, TimelineItemView
from autowonder.workitems.timeline import timeline, unified_timeline
from autowonder.workitems.watchers import follow, list_watchers, unfollow
from autowonder.workspaces.models import OrgMember
from tests.unit.test_workitems import MemorySession

_NOW = datetime(2026, 9, 24, 8, 0, 0)
_AONE_MENTION = (
    "<article class=\"4ever-article\"><p><span data-type=\"text\"></span>"
    "<span data-type=\"mention\" data-login=\"WORKER_1783582374386\">"
    "@Terraform-PD数字人(WORKER_1783582374386)</span>"
    "<span data-type=\"text\">看下截图报告为什么降级</span></p></article>"
)


def _rows(session: MemorySession, kind: type[object]) -> list[object]:
    return [row for row in session.rows if isinstance(row, kind)]


async def _workspace() -> MemorySession:
    session = MemorySession()
    session._next_id = 1000
    session.add(
        User(
            id=7,
            username="ada",
            nickname="艾达",
            password_hash="hash",
            is_deleted=0,
            status=0,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    session.add(
        User(
            id=8,
            username="bea",
            nickname="贝亚",
            password_hash="hash",
            is_deleted=0,
            status=0,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    session.add(
        OrgMember(
            id=1,
            tenant_id=100,
            user_id=7,
            status=0,
            is_deleted=0,
            access_level="READ_WRITE",
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    session.add(
        OrgMember(
            id=2,
            tenant_id=100,
            user_id=8,
            status=0,
            is_deleted=0,
            access_level="READ_WRITE",
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    session.add(
        Workitem(
            id=50,
            tenant_id=100,
            work_type="TASK",
            title="交付",
            version=0,
            is_deleted=0,
            creator_id=7,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    session.add(
        WorkitemComment(
            id=600,
            tenant_id=100,
            source_type="WORKITEM",
            workitem_id=50,
            author_type="HUMAN",
            author_ref=7,
            content_md="原文",
            gmt_create=_NOW,
        )
    )
    session.add(_agent(40013, "AW全栈开发", 40075))
    session.add(
        AgentVersion(
            id=40075,
            tenant_id=100,
            agent_id=40013,
            version_no=1,
            status="APPROVED",
            sdlc_id=810,
            is_deleted=0,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    session.add(
        SdlcStep(
            id=811,
            tenant_id=100,
            sdlc_id=810,
            step_order=1,
            name="入口",
            is_deleted=0,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    await session.flush()
    return session


def _agent(agent_id: int, name: str, online_version_id: int | None) -> Agent:
    return Agent(
        id=agent_id,
        tenant_id=100,
        name=name,
        online_version_id=online_version_id,
        is_deleted=0,
        version=0,
        gmt_create=_NOW,
        gmt_modified=_NOW,
    )


def test_mention_names_follow_guidance_patterns() -> None:
    """富文本 mention 去掉 worker 后缀，纯文本 @ 认汉字右边界。"""
    assert mention_names(_AONE_MENTION) == ["Terraform-PD数字人"]
    spaced = (
        "<article><p><span data-type=\"text\">"
        "@Terraform-PD数字人&nbsp;看一下</span></p></article>"
    )
    assert mention_names(spaced) == ["Terraform-PD数字人"]
    assert text_mention_index("请@Terraform-PD数字人处理一下", "Terraform-PD数字人") == 1
    html_only = (
        "<span data-type=\"mention\">@AW全栈开发(WORKER_1)</span>"
    )
    assert mention_comparable_content(html_only) == "@AW全栈开发"
    assert mention_comparable_content("@AW全栈开发 在吗") == "@AW全栈开发 在吗"


def test_participant_json_uses_is_agent() -> None:
    """参与者布尔字段序列化成 Jackson 的 isAgent。"""
    body = ParticipantView(user_id=4, name="构建员", agent=True).model_dump(by_alias=True)
    assert body["isAgent"] is True
    assert body["userId"] == 4


async def test_follow_is_idempotent_and_hides_inactive_members() -> None:
    """重复关注不新增行。失去空间成员资格的人不出现在关注列表。"""
    session = await _workspace()
    first = await follow(session, 50, 100, 7)
    second = await follow(session, 50, 100, 7)
    assert first.watched is True
    assert second.watcher_count == 1
    assert len(_rows(session, WorkitemWatcher)) == 1
    people = await list_watchers(session, 50, 100)
    assert people[0].role_name == "关注人"
    assert people[0].name == "艾达"
    member = _rows(session, OrgMember)[0]
    assert isinstance(member, OrgMember)
    member.status = 2
    assert await list_watchers(session, 50, 100) == []
    member.status = 0
    cleared = await unfollow(session, 50, 100, 7)
    assert cleared.watched is False
    assert cleared.watcher_count == 0
    again = await unfollow(session, 50, 100, 7)
    assert again.watched is False


async def test_explicit_human_mention_skips_the_author() -> None:
    """显式 @ 必须是空间成员。作者自己不会收到通知。"""
    session = await _workspace()
    _comment, notices = await add_comment(session, 50, "看一下", [8, 7], 100, 7)
    assert [notice.recipient_user_id for notice in notices] == [8]
    assert notices[0].actor_display_name == "艾达(7)"
    mentions = _rows(session, WorkitemCommentMention)
    assert len(mentions) == 2
    assert isinstance(mentions[0], WorkitemCommentMention)
    assert mentions[0].display_name_snapshot == "贝亚"
    try:
        await add_comment(session, 50, "看一下", [9], 100, 7)
    except BizError as error:
        assert error.error_code == ErrorCode.WORKSPACE_NOT_MEMBER
    else:
        raise AssertionError("expected missing member")


async def test_unpublished_agent_fails_without_dispatch() -> None:
    """没有在线版本时投递失败，并且不插入 PENDING 调度。"""
    session = await _workspace()
    session.add(_agent(40044, "AW代码冲突解决工程师", None))
    await session.flush()
    await create_for_comment(session, 100, 50, 600, "@AW代码冲突解决工程师", [40044], 7)
    deliveries = _rows(session, WorkitemCommentDelivery)
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert isinstance(delivery, WorkitemCommentDelivery)
    assert delivery.status == "FAILED"
    assert delivery.error == "目标数字员工未发布在线版本，无法启动会话"
    assert delivery.dispatch_id is None
    assert _rows(session, Dispatch) == []


async def test_first_mention_starts_formal_step() -> None:
    """只点名且没有历史调度时，转入入口步骤并写下正式确认。"""
    session = await _workspace()
    await create_for_comment(session, 100, 50, 600, "@AW全栈开发", [40013], 7)
    workitem = _rows(session, Workitem)[0]
    assert isinstance(workitem, Workitem)
    assert workitem.assignee_type == "AGENT"
    assert workitem.assignee_ref == 40013
    assert workitem.sdlc_id == 810
    assert workitem.current_step_id == 811
    delivery = _rows(session, WorkitemCommentDelivery)[0]
    assert isinstance(delivery, WorkitemCommentDelivery)
    assert delivery.status == "APPLIED"
    dispatches = _rows(session, Dispatch)
    assert len(dispatches) == 1
    dispatch = dispatches[0]
    assert isinstance(dispatch, Dispatch)
    assert dispatch.status == "PENDING"
    assert dispatch.idempotency_key == "50:811:1"
    assert dispatch.attempt == 1
    assert dispatch.resume_mode is None
    comments = _rows(session, WorkitemComment)
    assert any(
        isinstance(row, WorkitemComment) and row.content_md == "收到，已转入正式工作流程。"
        for row in comments
    )


async def test_instruction_starts_canonical_interaction() -> None:
    """第一次点名如果还带说明，先开正式会话而不是入口步骤。"""
    session = await _workspace()
    await create_for_comment(session, 100, 50, 600, "@AW全栈开发 在吗", [40013], 7)
    workitem = _rows(session, Workitem)[0]
    assert isinstance(workitem, Workitem)
    assert workitem.assignee_type is None
    delivery = _rows(session, WorkitemCommentDelivery)[0]
    assert isinstance(delivery, WorkitemCommentDelivery)
    assert delivery.status == "QUEUED"
    dispatch = _rows(session, Dispatch)[0]
    assert isinstance(dispatch, Dispatch)
    assert dispatch.resume_mode == "CANONICAL_INTERACTION"
    assert dispatch.idempotency_key == "guidance:" + str(delivery.id)
    assert dispatch.status == "PENDING"


async def test_running_worker_forks_side_interaction() -> None:
    """进行中且能恢复会话时，评论走旁路交互。"""
    session = await _workspace()
    session.add(
        Dispatch(
            id=91,
            tenant_id=100,
            source_type="WORKITEM",
            workitem_id=50,
            agent_id=40013,
            status="RUNNING",
            attempt=1,
            idempotency_key="50:root:1",
            is_deleted=0,
            version=0,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    session.add(
        DispatchRecoveryCheckpoint(
            id=3,
            tenant_id=100,
            workitem_id=50,
            dispatch_id=91,
            agent_id=40013,
            checkpoint_seq=1,
            provider_session_id="sess-91",
            oss_ref="oss://checkpoint",
            sha256="abc",
            size_bytes=4,
            gmt_create=_NOW,
        )
    )
    await session.flush()
    await create_for_comment(session, 100, 50, 600, "@AW全栈开发 在吗", [40013], 7)
    created = [
        row
        for row in _rows(session, Dispatch)
        if isinstance(row, Dispatch) and row.id != 91
    ]
    assert len(created) == 1
    assert isinstance(created[0], Dispatch)
    assert created[0].resume_mode == "SIDE_INTERACTION"
    assert created[0].resume_from_dispatch_id == 91
    assert created[0].status == "PENDING"


async def test_plain_text_mention_resolves_unique_agent() -> None:
    """没有空格的 @名称 也能命中租户里唯一的数字员工。"""
    session = await _workspace()
    session.add(_agent(40037, "Terraform-PD数字人", 40075))
    await session.flush()
    await create_for_comment(session, 100, 50, 600, "请@Terraform-PD数字人处理一下", None, 7)
    delivery = _rows(session, WorkitemCommentDelivery)[0]
    assert isinstance(delivery, WorkitemCommentDelivery)
    assert delivery.target_agent_id == 40037


async def test_ambiguous_mention_creates_no_guidance() -> None:
    """同名数字员工不止一个时，不写投递。"""
    session = await _workspace()
    session.add(_agent(40016, "AW全栈开发", 40075))
    await session.flush()
    await create_for_comment(session, 100, 50, 600, "请 @AW全栈开发 看一下", None, 7)
    assert _rows(session, WorkitemCommentDelivery) == []


async def test_missing_source_dispatch_is_not_found() -> None:
    """交互来源调度不属于该工单时返回 17030。"""
    session = await _workspace()
    try:
        await enqueue_comment_interaction(session, 100, 50, 40013, 999, False, 811, 701, 7)
    except BizError as error:
        assert error.error_code == ErrorCode.DISPATCH_NOT_FOUND
        assert error.code == "17030"
    else:
        raise AssertionError("expected missing dispatch")


async def test_workitem_enqueue_is_idempotent() -> None:
    """同一工单、步骤和尝试次数只保留一条调度。"""
    session = await _workspace()
    first = await enqueue_workitem(session, 100, 50, 811, 40013, 1, 7)
    second = await enqueue_workitem(session, 100, 50, 811, 40013, 1, 7)
    assert first.id == second.id
    assert len(_rows(session, Dispatch)) == 1


async def test_timeline_labels_and_interaction_overlay() -> None:
    """事件文案、投递状态和失败调度覆盖与 Java 向量一致。"""
    session = await _workspace()
    session.add(
        WorkitemEvent(
            id=1,
            tenant_id=100,
            workitem_id=50,
            event_type="CREATE",
            to_val="new",
            actor_type="HUMAN",
            actor_ref=7,
            gmt_create=_NOW,
        )
    )
    session.add(
        WorkitemCommentDelivery(
            id=701,
            tenant_id=100,
            source_type="WORKITEM",
            workitem_id=50,
            comment_id=600,
            target_agent_id=40013,
            status="DELIVERED",
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    await session.flush()
    events = await timeline(session, 50)
    assert events[0].event_type == "CREATE"
    assert events[0].to_val_display == "new"
    assert events[0].actor_display_name == "艾达(7)"
    items = await unified_timeline(session, 50)
    await attach_interaction_statuses(session, 100, 50, items)
    comment = [item for item in items if item.type == "comment"][0]
    assert comment.interactions is not None
    assert comment.interactions[0].status == "DELIVERED"
    assert comment.interactions[0].target_agent_id == 40013
    delivery = _rows(session, WorkitemCommentDelivery)[0]
    assert isinstance(delivery, WorkitemCommentDelivery)
    delivery.status = "QUEUED"
    delivery.dispatch_id = 91
    session.add(
        Dispatch(
            id=91,
            tenant_id=100,
            source_type="WORKITEM",
            workitem_id=50,
            agent_id=40013,
            status="CANCELED",
            error="已取消",
            attempt=1,
            idempotency_key="canceled",
            is_deleted=0,
            version=0,
            gmt_create=_NOW,
            gmt_modified=_NOW,
        )
    )
    await session.flush()
    fresh = [
        TimelineItemView(id=600, type="comment", gmt_create=_NOW),
    ]
    await attach_interaction_statuses(session, 100, 50, fresh)
    assert fresh[0].interactions is not None
    assert fresh[0].interactions[0].status == "CANCELED"
    assert fresh[0].interactions[0].error == "已取消"
