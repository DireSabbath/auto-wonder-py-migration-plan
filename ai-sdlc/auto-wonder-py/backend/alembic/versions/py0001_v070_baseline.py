"""V070 基线 stamp。本修订不产生 DDL。"""

revision = "py0001_v070"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """schema 已由 initdb SQL 创建，这里只占位版本。"""


def downgrade() -> None:
    """不回滚 Java 侧 schema。"""
