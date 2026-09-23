"""用户与认证接口的请求和响应。"""

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
