"""迁移缺口里可以脱离数据库核对的通知、回读和 Stream 规则。"""

from types import SimpleNamespace

import pytest

from autowonder.integrations.aone_api import ExternalComment
from autowonder.integrations.comment_readback import (
    COMMENT_NOT_FOUND,
    find_marked_comment,
    marker_for_receipt,
    readback_supported,
)
from autowonder.integrations.dingtalk.stream import (
    BOT_TOPIC,
    CREDENTIAL_UNREADABLE,
    callback_ack,
    ensure_started,
    frame_kind,
    inbound_text,
    open_connection_body,
    system_ack,
)
from autowonder.integrations.operation_keys import operation_marker
from autowonder.notifications.service import absolute_notice_link, notification_markdown
from autowonder.scheduledtasks.notify import attention_copy, attention_event, notify_owner
from autowonder.workspaces.access_requests import access_request_content, access_review_content


def test_assignment_and_access_copy() -> None:
    """指派和加入申请的正文带上人和空间。"""
    assert access_request_content("林夏", "交付组") == "林夏 申请加入「交付组」"
    assert access_review_content("周衡", "交付组", True, None) == (
        "周衡已通过你加入「交付组」的申请"
    )
    refused = access_review_content("周衡", "交付组", False, "名额已满")
    assert refused == "周衡已拒绝你加入「交付组」的申请：名额已满"


def test_im_markdown_uses_public_base() -> None:
    """站内路径拼成绝对链接，绝对地址保持原样。"""
    text = notification_markdown(
        "工单已指派给你",
        "周衡 将「登录失败」指派给了你",
        "/workitems/9",
        "http://localhost:7002/",
    )
    assert text == (
        "## 工单已指派给你\n\n周衡 将「登录失败」指派给了你\n\nhttp://localhost:7002/workitems/9"
    )
    assert absolute_notice_link("https://example.test/a", "http://localhost:7002") == (
        "https://example.test/a"
    )


def test_scheduled_attention_copy() -> None:
    """失败和需要人工处理通知负责人。负责人自己暂停时不通知。"""
    failed = attention_event("FAILED")
    assert failed is not None
    assert failed[0] == "SCHEDULED_RUN_FAILED"
    assert attention_event("SUCCEEDED") is None
    assert notify_owner("FAILED", 4, 4) is True
    assert notify_owner("PAUSED", 4, 4) is False
    assert notify_owner("PAUSED", 8, 4) is True
    assert notify_owner("NEEDS_HUMAN", 0, 4) is True
    text = attention_copy("夜间巡检", "定时任务运行失败", "x" * 200)
    assert text.startswith("「夜间巡检」定时任务运行失败：")
    assert len(text) <= 1024
    assert len(text.split("：", 1)[1]) == 180


def test_aone_readback_matches_marker_only() -> None:
    """JOBDUAL 保持不可回读。Aone 评论忽略已删除行，并认出隐藏标记。"""
    assert readback_supported("JOBDUAL", "COMMENT_CREATE", True) is False
    assert readback_supported("AONE", "COMMENT_CREATE", False) is False
    assert readback_supported("AONE", "COMMENT_CREATE", True) is True
    assert readback_supported("AONE", "STATUS_UPDATE", True) is False
    marker = operation_marker("aone.comment:abc")
    assert marker_for_receipt({"marker": " <!-- kept --> "}, "other") == " <!-- kept --> "
    assert marker_for_receipt({}, "aone.comment:abc") == marker
    deleted = ExternalComment(
        external_id="1",
        content_md="hello " + marker,
        source_status="DELETED",
    )
    active = ExternalComment(
        external_id="2",
        content_md="hello " + marker,
        source_status="ACTIVE",
    )
    assert find_marked_comment([deleted, active], marker) == active
    assert find_marked_comment([deleted], marker) is None
    assert COMMENT_NOT_FOUND == "Aone comment was not found on readback"


def test_dingtalk_stream_frames() -> None:
    """开连接只订机器人话题。文本入站要有消息号、会话和正文。"""
    body = open_connection_body("app", "secret")
    assert body["subscriptions"] == [{"type": "CALLBACK", "topic": BOT_TOPIC}]
    ping = {
        "type": "SYSTEM",
        "headers": {"topic": "ping", "messageId": "m1"},
        "data": '{"opaque":"abc"}',
    }
    assert frame_kind(ping) == "ping"
    ack = system_ack(ping)
    assert ack["code"] == 200
    assert ack["data"] == '{"opaque": "abc"}'
    bot = {
        "type": "CALLBACK",
        "headers": {"topic": BOT_TOPIC, "messageId": "m2"},
    }
    assert frame_kind(bot) == "bot"
    assert callback_ack(bot, 200)["data"] == '{"response": "OK"}'
    assert callback_ack(bot, 500)["code"] == 500
    raw = '{"msgId":"mid","conversationId":"cid","msgtype":"text","text":{"content":" 你好 "}}'
    assert inbound_text(raw) == ("mid", "cid", "你好")
    assert inbound_text('{"msgtype":"picture","msgId":"mid","conversationId":"cid"}') is None
    assert inbound_text("not-json") is None


async def test_unreadable_stream_credential_stays_failed() -> None:
    """凭据解不开时停在 FAILED，不留下后台重连。"""
    writes: list[tuple[int, str, str | None]] = []

    async def write(binding_id: int, status: str, error: str | None) -> None:
        writes.append((binding_id, status, error))

    row = SimpleNamespace(
        id=7,
        credential_ref="x",
        app_key="app",
        tenant_id=1,
        agent_id=2,
        base_url=None,
    )
    with pytest.raises(ValueError):
        await ensure_started(row, write)
    assert writes == [
        (7, "CONNECTING", None),
        (7, "FAILED", CREDENTIAL_UNREADABLE),
    ]
