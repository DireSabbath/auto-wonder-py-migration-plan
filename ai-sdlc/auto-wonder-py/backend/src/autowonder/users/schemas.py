"""用户与认证接口的请求和响应。"""

from datetime import datetime

from autowonder.core.schema import ApiModel


class RegisterRequest(ApiModel):
    """注册请求。"""

    username: str | None = None
    password: str | None = None
    email: str | None = None
    nickname: str | None = None


class LoginRequest(ApiModel):
    """登录请求。"""

    username: str | None = None
    password: str | None = None


class LogoutRequest(ApiModel):
    """退出登录。refreshToken 可空，有值时吊销对应刷新令牌。"""

    refresh_token: str | None = None


class RefreshRequest(ApiModel):
    """用刷新令牌换访问令牌，并可声明工作空间。"""

    refresh_token: str | None = None
    workspace_id: int | None = None


class UserView(ApiModel):
    """对外用户信息。``is_admin`` 按登录时的数据库标志读取。"""

    id: int | None = None
    username: str | None = None
    nickname: str | None = None
    email: str | None = None
    is_admin: bool | None = None


class LoginResponse(ApiModel):
    """登录成功后的令牌与用户信息。"""

    user_id: int
    access_token: str
    refresh_token: str
    user: UserView


class RefreshResponse(ApiModel):
    """刷新后的访问令牌。"""

    access_token: str


class ChangePasswordRequest(ApiModel):
    """修改当前用户口令。"""

    old_password: str | None = None
    new_password: str | None = None


class DeactivationRequest(ApiModel):
    """申请注销时必须再次输入用户名。"""

    confirm_username: str | None = None


class DeactivationStatusView(ApiModel):
    """注销状态。布尔字段始终返回，时间只在冷静期内出现。"""

    pending: bool = False
    deactivated_at: datetime | None = None
    cooling_off_expires_at: datetime | None = None
    revoked: bool = False


class UpsertUserSettingRequest(ApiModel):
    """写入一条用户偏好。空请求表示清空取值。"""

    value_json: str | None = None


class UserSettingView(ApiModel):
    """一条用户偏好。``value_json`` 是 JSON 文本，未设置时为 null。"""

    key: str
    value_json: str | None = None
