"""分类名称、路径、层级和 parentId 解析。这些检查不访问数据库。"""

from fastapi.testclient import TestClient

from autowonder.categories.models import AssetCategory
from autowonder.categories.schemas import create_fields_from_json, update_fields_from_json
from autowonder.categories.service import (
    collect_descendants,
    normalized_name,
    path_of,
    require_name,
    require_parent,
    sibling_conflicts,
    subtree_height,
    trim_to_null,
)
from autowonder.core.errors import BizError, ErrorCode
from autowonder.main import create_app


def _node(category_id: int, parent_id: int | None, name: str) -> AssetCategory:
    return AssetCategory(id=category_id, parent_id=parent_id, name=name)


def test_category_name_parent_path_and_depth() -> None:
    """空白名称拒绝；路径按父链拼接；自己不算重名；第五层允许、第六层拒绝靠高度计算。"""
    assert require_name("  前端  ") == "前端"
    try:
        require_name(" ")
    except BizError as error:
        assert error.error_code == ErrorCode.CATEGORY_NAME_REQUIRED
    else:
        raise AssertionError("expected blank category name")
    try:
        require_name("名" * 129)
    except BizError as error:
        assert str(error) == "分类名称最长 128 个字符"
    else:
        raise AssertionError("expected long category name")
    assert require_name("名" * 128) == "名" * 128
    assert require_parent(None) is None
    try:
        require_parent(0)
    except BizError as error:
        assert str(error) == "上级分类不合法"
    else:
        raise AssertionError("expected invalid parent")
    assert trim_to_null("  ") is None
    assert trim_to_null(" 说明 ") == "说明"
    assert normalized_name(" Vue ") == "vue"
    assert sibling_conflicts(True, 10000, 10000) is False
    assert sibling_conflicts(True, 2, 3) is True
    assert sibling_conflicts(False, None, None) is False
    tree = {
        1: _node(1, None, "编码"),
        2: _node(2, 1, "前端"),
        3: _node(3, 2, "页面"),
    }
    assert path_of(tree[3], tree) == "编码 → 前端 → 页面"
    assert subtree_height(1, tree) == 3
    ids = [1]
    collect_descendants(tree, 1, ids)
    assert ids == [1, 2, 3]
    created = create_fields_from_json({"name": "库", "parentId": None, "description": 1})
    assert created.parent_id is None
    assert created.description == "1"
    try:
        create_fields_from_json({"parentId": 0})
    except BizError as error:
        assert error.error_code == ErrorCode.PARAM_INVALID
        assert str(error) == "参数不合法"
    else:
        raise AssertionError("expected non-positive create parent")
    updated = update_fields_from_json({"name": "库", "parentId": None})
    assert updated.name_present is True
    assert updated.parent_id_present is True
    assert updated.parent_id is None
    assert updated.description_present is False
    try:
        update_fields_from_json({"parentId": True})
    except BizError as error:
        assert str(error) == "parentId 必须是正整数或 null"
    else:
        raise AssertionError("expected invalid update parent")


def test_category_routes_match_java_and_require_login() -> None:
    """路径名与 Java 一致，未带令牌时返回 401。"""
    client = TestClient(create_app())
    paths = client.app.openapi()["paths"]
    assert "/api/categories" in paths
    assert "/api/categories/{id}" in paths
    response = client.get("/api/categories")
    assert response.status_code == 401
    assert response.json()["code"] == "10401"
