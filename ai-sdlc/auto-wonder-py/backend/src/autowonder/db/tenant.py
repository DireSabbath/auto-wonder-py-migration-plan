"""工作空间隔离。

Java 的 TenantSqlRewriter 只改写单表 PlainSelect。这里用
``with_loader_criteria`` 覆盖 ORM 查询的全部形态，严格性只增不减。
这 38 张表的清单以 ``tenant_tables.py`` 为准，由 sync 脚本从
``TenantTables.java`` 拷出。INSERT 不自动写入 ``tenant_id``。
"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from autowonder.core.context import current_workspace_id
from autowonder.db.tenant_tables import TENANT_TABLES

TENANT_MODELS: list[type] = []
__all__ = [
    "TENANT_MODELS",
    "TENANT_TABLES",
    "install_tenant_criteria",
    "register_tenant_model",
]
_INSTALLED = False


def register_tenant_model(model: type) -> None:
    """登记一张需要按 workspace 过滤的 ORM 模型。"""
    if model not in TENANT_MODELS:
        TENANT_MODELS.append(model)


def install_tenant_criteria() -> None:
    """在 ORM SELECT 上注入 tenant_id 条件。"""
    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(Session, "do_orm_execute", _apply_tenant_criteria)
    _INSTALLED = True


def _apply_tenant_criteria(execute_state: Any) -> None:
    workspace_id = current_workspace_id()
    if workspace_id is None or not execute_state.is_select:
        return
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return
    if not TENANT_MODELS:
        return
    options = [_tenant_criteria(model, workspace_id) for model in TENANT_MODELS]
    execute_state.statement = execute_state.statement.options(*options)


def _tenant_criteria(model: type, workspace_id: int) -> Any:
    def match_tenant(cls: Any, bound_workspace_id: int = workspace_id) -> Any:
        return cls.tenant_id == bound_workspace_id

    return with_loader_criteria(model, match_tenant, include_aliases=True)
