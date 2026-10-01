"""Accepted node executors; capability selection never grants authority.

V1 implements agent execution for story development. Additional executors need
an implemented adapter and an explicit compatibility entry before admission.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .developer_personas import DEVELOPER_PERSONAS


class ExecutorAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["agent"]
    role: Literal["develop"]
    persona: str = Field(min_length=1, max_length=128)


def validate_executor(node_kind, assignment):
    if node_kind != "story" or assignment.role != "develop" or assignment.persona not in DEVELOPER_PERSONAS:
        raise ValueError("unsupported_node_executor: only story development with a supported developer persona is implemented")


def selected_executor(document, address, *, default, accepted):
    """Read an exact assignment; even a default-valued assignment needs acceptance."""
    matches = [node for node in document.get("nodes", []) if node.get("address") == address]
    if not matches:
        return default
    if len(matches) != 1:
        raise ValueError("executor_assignment_ambiguous")
    value = matches[0].get("executor")
    if value is None:
        return default
    if not accepted:
        raise ValueError("executor_assignment_not_accepted")
    assignment = ExecutorAssignment.model_validate(value)
    validate_executor(matches[0].get("kind"), assignment)
    return assignment.persona


async def accepted_executor_persona(session, node, *, default):
    from sqlalchemy import select

    from .models import OrchestrationAcceptedPlan, OrchestrationFlow

    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == node.org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    if plan is None:
        return default
    flow = await session.get(OrchestrationFlow, node.flow_id)
    if flow is None or flow.org_id != node.org_id:
        raise ValueError("executor_assignment_flow_missing")
    address = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
    document = plan.plan_document or {}
    for declaration in document.get("nodes", []):
        if declaration.get("address") == address and declaration.get("executor") is not None and declaration.get("kind") != node.kind:
            raise ValueError("executor_assignment_node_changed")
    return selected_executor(document, address, default=default, accepted=bool(plan.accepted_by_decision_id))
