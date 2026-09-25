"""入站回执里不依赖数据库的判定。"""

from datetime import UTC, datetime

from autowonder.conversations.elicitation import elicitation_terminal_status
from autowonder.conversations.runtime_reports import ack_reply_content, outbound_reply_content
from autowonder.executors.catalog import catalog_ticket_matches, normalize_catalog_models
from autowonder.executors.restart import (
    apply_restart_result,
    complete_restart,
    parse_started_at,
)
from autowonder.executors.upgrade import upgrade_phase_target


def test_restart_result_keeps_the_active_request() -> None:
    """只有同一次进行中的重启接受阶段；更早的启动时间不能算完成。"""
    state = {"requestId": "req-1", "status": "REQUESTED", "previousStartedAt": 1_000}
    updated = apply_restart_result(state, "req-1", "RESTARTING", "going")
    assert updated is not None
    assert updated["status"] == "RESTARTING"
    assert updated["message"] == "going"
    assert apply_restart_result(state, "other", "FAILED", None) is None
    done = complete_restart(updated, "req-1", 2_000, "2026-09-24T00:00:00Z")
    assert done is not None
    assert done["status"] == "COMPLETED"
    assert complete_restart(updated, "req-1", 1_000, "2026-09-24T00:00:00Z") is None


def test_started_at_rejects_far_future_and_ancient_values() -> None:
    """启动时间超出允许窗口时，心跳不改重启状态。"""
    now = datetime(2026, 9, 24, tzinfo=UTC)
    assert parse_started_at("2026-09-24T00:00:00Z", now) is not None
    assert parse_started_at("2019-12-31T00:00:00Z", now) is None
    assert parse_started_at("not-a-time", now) is None


def test_upgrade_phase_names() -> None:
    """升级阶段名和 Java 的状态推进一致。"""
    assert upgrade_phase_target("accepted") == "postpone"
    assert upgrade_phase_target("draining") == "DRAINING"
    assert upgrade_phase_target("downloading") == "UPDATING"
    assert upgrade_phase_target("applying") == "UPDATING"
    assert upgrade_phase_target("success") == "SUCCESS"
    assert upgrade_phase_target("failed") == "FAILED"
    assert upgrade_phase_target("other") is None


def test_ack_reply_falls_back_when_the_runtime_is_silent() -> None:
    """没有正文时，失败带原因，取消用终止文案。"""
    assert outbound_reply_content("  ", "boom") == "回复失败：boom"
    assert outbound_reply_content(None, None) == "（数字人未返回内容）"
    assert ack_reply_content("canceled", None, "x") == "响应已终止"
    assert ack_reply_content("SUCCESS", "done", None) == "done"


def test_catalog_ticket_and_model_normalization() -> None:
    """票据必须三者一致；重复或空白模型 id 丢掉。"""
    ticket = {"tenantId": 1, "executorId": 2, "provider": "qoder"}
    assert catalog_ticket_matches(ticket, 1, 2, "qoder") is True
    assert catalog_ticket_matches(ticket, 1, 2, "other") is False
    assert normalize_catalog_models(
        [
            {"id": " a ", "name": " A "},
            {"id": "a", "name": "dup"},
            {"id": "", "name": "skip"},
            "nope",
        ]
    ) == [{"id": "a", "name": "A"}]


def test_elicitation_terminal_status() -> None:
    """问答卡片只认 accept、decline 和 cancel。"""
    assert elicitation_terminal_status("accept") == "ANSWERED"
    assert elicitation_terminal_status("decline") == "DECLINED"
    assert elicitation_terminal_status("cancel") == "CANCELED"
    assert elicitation_terminal_status("ask") is None
