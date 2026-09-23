"""刷新令牌与访问令牌 jti 黑名单，键前缀与 Java ``SessionService`` 相同。"""

from redis.asyncio import Redis

REFRESH_PREFIX = "auth:refresh:"
BLACKLIST_PREFIX = "jwt:blacklist:"


class SessionService:
    """把刷新令牌和吊销记录放在 Redis。"""

    def __init__(self, client: Redis) -> None:
        self.client = client

    async def store_refresh(self, refresh_token: str, user_id: int, ttl_seconds: int) -> None:
        """保存刷新令牌到用户 id 的映射。"""
        await self.client.set(REFRESH_PREFIX + refresh_token, str(user_id), ex=ttl_seconds)

    async def get_user_id_by_refresh(self, refresh_token: str) -> int | None:
        """读取刷新令牌对应的用户；不存在时返回 None。"""
        value = await self.client.get(REFRESH_PREFIX + refresh_token)
        if value is None:
            return None
        return int(value)

    async def revoke_refresh(self, refresh_token: str) -> None:
        """删除刷新令牌。"""
        await self.client.delete(REFRESH_PREFIX + refresh_token)

    async def blacklist_jti(self, jti: str, ttl_seconds: int) -> None:
        """在访问令牌剩余寿命内拒绝该 jti。"""
        await self.client.set(BLACKLIST_PREFIX + jti, "1", ex=ttl_seconds)

    async def is_blacklisted(self, jti: str) -> bool:
        """jti 是否已被吊销。"""
        return bool(await self.client.exists(BLACKLIST_PREFIX + jti))
