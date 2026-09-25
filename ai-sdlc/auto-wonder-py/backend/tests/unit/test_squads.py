"""小队与模板契约中不依赖数据库的规则。"""

from datetime import datetime

from fastapi.testclient import TestClient

from autowonder.core.errors import BizError, ErrorCode
from autowonder.core.result import dump_data
from autowonder.executors.registry import drop_session, is_online, presence, register_session
from autowonder.main import create_app
from autowonder.squads.attribution import SquadRefs, assemble_refs
from autowonder.squads.schemas import SquadView
from autowonder.squads.service import page_window, reject_blank_squad_name, require_squad_name
from autowonder.templates.content import (
    agent_details,
    is_system_template,
    split_tags,
    squad_info,
    step_required,
)
from autowonder.templates.schemas import SquadTemplateView


def test_squad_name_and_page_window() -> None:
    """空白名称拒绝，分页窗口与 Java 的 page/size 修正一致。"""
    assert require_squad_name("  交付小队  ") == "交付小队"
    try:
        require_squad_name("   ")
    except BizError as error:
        assert error.error_code == ErrorCode.SQUAD_NAME_REQUIRED
    else:
        raise AssertionError("expected blank squad name")
    try:
        reject_blank_squad_name("")
    except BizError as error:
        assert error.error_code == ErrorCode.SQUAD_NAME_REQUIRED
    else:
        raise AssertionError("expected blank update name")
    reject_blank_squad_name(None)
    assert page_window(0, 0) == (0, 20)
    assert page_window(2, 1000) == (100, 100)
    assert page_window(1, 100) == (0, 100)


def test_squad_card_json_names() -> None:
    """列表卡片保留 null 的成员与流程，debug 开关按布尔值写出。"""
    created = dump_data(
        SquadView(
            id=8,
            name="交付小队",
            debug_log_enabled=False,
            member_agent_ids=[],
            member_count=0,
        )
    )
    assert created["debugLogEnabled"] is False
    assert created["memberAgentIds"] == []
    assert created["memberCount"] == 0
    assert created["roleCount"] == 0
    assert created["sdlcs"] is None
    assert created["executors"] is None
    stamp = dump_data(datetime(2026, 9, 23, 8, 0, 0))
    assert stamp == "2026-09-23T00:00:00.000+00:00"


def test_template_tags_steps_and_system_flag() -> None:
    """标签按逗号拆开，缺省 required 视为必需，系统模板看 tenant。"""
    assert split_tags(None) == []
    assert split_tags("推荐,快速") == ["推荐", "快速"]
    assert step_required({}) == 1
    assert step_required({"required": False}) == 0
    assert step_required({"required": True}) == 1
    assert is_system_template(None) is True
    assert is_system_template(4) is False
    content = {
        "squad": {"name": "独立开发者小队", "description": "单人闭环"},
        "agents": [
            {
                "name": "全栈开发",
                "roleCode": "FS_DEV",
                "roleName": "前后端全栈开发",
                "responsibilities": "交付",
                "sdlc": {
                    "name": "独立开发者 SDLC",
                    "description": "四步",
                    "steps": [{"order": 1, "name": "需求分析", "kind": "WORK"}],
                },
            }
        ],
    }
    assert squad_info(content).name == "独立开发者小队"
    agent = agent_details(content)[0]
    assert agent.role_code == "FS_DEV"
    assert agent.sdlc is not None
    assert agent.sdlc.steps[0].kind == "WORK"
    payload = dump_data(SquadTemplateView(id=1, name="独立开发者", tags=["推荐"], system=True))
    assert payload["system"] is True
    assert payload["squadSize"] is None


def test_deleted_squads_drop_out_of_attribution() -> None:
    """已删除小队没有名称，引用里不再出现。"""
    refs = assemble_refs({7: {3: None, 9: None}}, {3: "交付小队"})
    assert refs[7] == SquadRefs(ids=[3], names=["交付小队"])


def test_executor_presence_defaults_offline() -> None:
    """没有接入会话的执行器在小队卡片上是 OFFLINE。"""
    assert is_online(42) is False
    assert presence(42) == "OFFLINE"
    register_session(42)
    try:
        assert presence(42) == "ONLINE"
    finally:
        drop_session(42)
    assert presence(42) == "OFFLINE"


def test_squad_routes_require_login() -> None:
    """未带令牌访问小队和模板接口时返回 401。"""
    client = TestClient(create_app())
    squads = client.get("/api/squads")
    templates = client.get("/api/squad-templates")
    assert squads.status_code == 401
    assert templates.status_code == 401
    assert squads.json()["code"] == "10401"
    assert templates.json()["code"] == "10401"
