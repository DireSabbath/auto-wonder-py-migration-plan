"""把学习增量里的 memory 条目沉淀成待审核记忆。坏载荷只记日志。"""

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from autowonder.memories.schemas import CreateMemoryRequest
from autowonder.memories.service import create_from_learning_delta

logger = logging.getLogger(__name__)


async def ingest(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    dispatch_id: int,
    payload: bytes,
) -> None:
    """解析 entries。type 为 memory 且正文非空时按调度条目幂等写入。"""
    try:
        root = json.loads(payload.decode("utf-8"))
        if not isinstance(root, dict):
            return
        entries = root.get("entries")
        if not isinstance(entries, list):
            return
        for index, entry in enumerate(entries):
            await _ingest_entry(session, tenant_id, agent_id, dispatch_id, index, entry)
    except Exception:
        logger.warning("memory sedimentation skipped dispatchId=%s", dispatch_id, exc_info=True)


async def _ingest_entry(
    session: AsyncSession,
    tenant_id: int,
    agent_id: int,
    dispatch_id: int,
    index: int,
    entry: object,
) -> None:
    if not isinstance(entry, dict):
        return
    if entry.get("type") != "memory":
        return
    content = entry.get("content")
    if not isinstance(content, str) or content.strip() == "":
        return
    title = entry.get("title")
    if not isinstance(title, str) or title.strip() == "":
        title = first_line(content)
    await create_from_learning_delta(
        session,
        CreateMemoryRequest(
            scope="AGENT",
            owner_ref=agent_id,
            type="memory",
            title=title,
            content_md=content,
        ),
        tenant_id,
        dispatch_id,
        index,
    )


def first_line(text: str) -> str:
    """取第一行并截到 200 个字符。开头就是换行时改用整段去空白。"""
    newline = text.find("\n")
    if newline > 0:
        line = text[:newline].strip()
    else:
        line = text.strip()
    if len(line) > 200:
        return line[:200]
    return line
