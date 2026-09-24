"""账号注销到期扫描，对应 ``AccountDeactivationExpiryTask``，默认 60 秒一次。"""

from autowonder.db.session import SessionLocal
from autowonder.users.deactivation import sweep_expired_deactivations


async def account_deactivation_expiry() -> int:
    """处理已经过冷静期的注销申请。"""
    async with SessionLocal() as session:
        return await sweep_expired_deactivations(session)
