"""SQLAlchemy 声明基类。各域模型在导入时注册到同一 metadata。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全部业务表共用的元数据注册点。"""
