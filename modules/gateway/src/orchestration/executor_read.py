"""Project executor display labels without exposing the approval record."""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .developer_personas import OPERATIONS_PERSONAS
from .models import OrchestrationAcceptedPlan


class OperationsExecutorSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["operate"]
    persona: Literal["operations", "agent-codex-operations"]


async def load_operations_executor_summaries(session: AsyncSession, *, org_id: str, flow_id: str) -> dict[str, OperationsExecutorSummary]:
    """Return only labels from the current accepted plan in this tenant and flow.

    These labels describe assigned work, not execution authority. Plan identity,
    approval attribution and arbitrary document fields stay behind this read model.
    """
    document = await session.scalar(
        select(OrchestrationAcceptedPlan.plan_document).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
            OrchestrationAcceptedPlan.accepted_by_decision_id.is_not(None),
        )
    )
    return operations_executor_summaries(document)


def operations_executor_summaries(document: dict | None) -> dict[str, OperationsExecutorSummary]:
    """Project labels without repeating the graph request's plan read."""
    return {
        entry["address"]: OperationsExecutorSummary(role="operate", persona=entry["executor"]["persona"])
        for entry in (document or {}).get("nodes", [])
        if isinstance(entry, dict)
        and entry.get("kind") == "story"
        and isinstance(entry.get("address"), str)
        and isinstance(entry.get("executor"), dict)
        and entry["executor"].get("role") == "operate"
        and entry["executor"].get("persona") in OPERATIONS_PERSONAS
    }
