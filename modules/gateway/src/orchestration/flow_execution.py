"""Flow admission switch. Existing workers and result reconciliation remain valid."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import OrchestrationFlow


async def flow_is_paused(session: AsyncSession, *, org_id: str, flow_id: str, lock: bool = False) -> bool:
    query = (
        select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id).execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    flow = await session.scalar(query)
    return flow is None or flow.execution_paused or flow.archived_at is not None
