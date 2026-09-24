"""API 模型基类：Python 内部 snake_case，JSON 使用 camelCase。"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """与前端契约对齐的请求/响应模型。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
