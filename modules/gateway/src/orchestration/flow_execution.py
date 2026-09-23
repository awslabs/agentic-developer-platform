"""Flow admission switch. Existing workers and result reconciliation remain valid."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import OrchestrationFlow


async def flow_is_paused(session: AsyncSession, *, org_id: str, flow_id: str, lock: bool = False) -> bool:
    query = select(OrchestrationFlow.execution_paused).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id)
    if lock:
        query = query.with_for_update()
    return (await session.scalar(query)) is not False
