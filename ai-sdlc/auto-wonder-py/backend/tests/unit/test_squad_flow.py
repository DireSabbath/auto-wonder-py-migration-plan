"""七角色小队闭环使用的角色集合。"""

from verify.squad_flow import FULL_CYCLE_ROLES, role_codes


def test_full_cycle_roles_match_the_plan() -> None:
    agents = [{"roleCode": code, "agentId": index} for index, code in enumerate(FULL_CYCLE_ROLES)]
    assert role_codes(agents) == set(FULL_CYCLE_ROLES)
    assert len(FULL_CYCLE_ROLES) == 7
    assert "REQ_CLARIFIER" in FULL_CYCLE_ROLES
    assert "DBA" in FULL_CYCLE_ROLES
