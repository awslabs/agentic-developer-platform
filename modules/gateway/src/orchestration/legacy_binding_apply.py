"""Operator-authorized migration of legacy Product metadata or untouched stories."""

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .amend import AmendmentContext, amend_plan, binding_reservations
from .compile import ApprovalContext, require_accessible_product
from .models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationLegacyMigration,
    OrchestrationNode,
    OrchestrationPendingAmendment,
    OrchestrationTrackerPublication,
)
from .proposal import LoopProposal, RepositoryConnection
from .repository import OrchestrationRepository
from .repository_binding import resolve_story_repository
from .state import ActorKind, NodeState


class LegacyMigrationError(ValueError):
    pass


class RepositoryTarget(BaseModel):
    node_id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=3, max_length=255)
    repository_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)


class LegacyMigrationRequest(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    expected_plan_version: int = Field(ge=0)
    expected_plan_hash: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")
    reason: str = Field(min_length=8, max_length=2000)
    product_id: str | None = Field(default=None, min_length=1, max_length=36)
    repository: RepositoryTarget | None = None

    @model_validator(mode="after")
    def one_change(self):
        if (self.product_id is None) == (self.repository is None):
            raise ValueError("provide exactly one explicit Product or repository target")
        return self


@dataclass(frozen=True)
class LegacyMigrationResult:
    receipt_id: str
    result_plan_version: int
    already_applied: bool


async def apply_legacy_migration(
    session: AsyncSession, *, org_id: str, flow_id: str, actor: ApprovalContext, request: LegacyMigrationRequest
) -> LegacyMigrationResult:
    if actor.org_id != org_id or ActorKind(actor.actor_kind) is not ActorKind.HUMAN:
        raise LegacyMigrationError("operator_authority_required")
    flow = await session.scalar(
        select(OrchestrationFlow).where(OrchestrationFlow.org_id == org_id, OrchestrationFlow.id == flow_id).with_for_update()
    )
    if flow is None:
        raise LegacyMigrationError("flow_not_found")
    payload = request.model_dump(mode="json")
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prior = await session.scalar(
        select(OrchestrationLegacyMigration).where(
            OrchestrationLegacyMigration.org_id == org_id,
            OrchestrationLegacyMigration.flow_id == flow_id,
            OrchestrationLegacyMigration.request_id == request.request_id,
        )
    )
    if prior is not None:
        if prior.request_hash != request_hash:
            raise LegacyMigrationError("request_id_reused")
        return LegacyMigrationResult(prior.id, prior.result_plan_version, True)
    plans = (
        await session.scalars(
            select(OrchestrationAcceptedPlan)
            .where(
                OrchestrationAcceptedPlan.org_id == org_id,
                OrchestrationAcceptedPlan.flow_id == flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    if len(plans) > 1:
        raise LegacyMigrationError("accepted_plan_conflict")
    plan = plans[0] if plans else None
    if (plan.version if plan else 0) != request.expected_plan_version or (plan.plan_hash if plan else "") != request.expected_plan_hash:
        raise LegacyMigrationError("accepted_plan_stale")
    if await session.scalar(
        select(OrchestrationAction.id)
        .join(OrchestrationExecution, OrchestrationExecution.id == OrchestrationAction.execution_id)
        .where(
            OrchestrationAction.org_id == org_id,
            OrchestrationExecution.org_id == org_id,
            OrchestrationExecution.flow_id == flow_id,
            OrchestrationAction.status.in_(("prepared", "dispatched", "unknown")),
        )
        .limit(1)
    ):
        raise LegacyMigrationError("pending_effect_reconciliation_required")
    if await session.scalar(
        select(OrchestrationPendingAmendment.id)
        .where(
            OrchestrationPendingAmendment.org_id == org_id,
            OrchestrationPendingAmendment.flow_id == flow_id,
            OrchestrationPendingAmendment.state == "pending",
        )
        .limit(1)
    ):
        raise LegacyMigrationError("pending_amendment_reconciliation_required")
    if await session.scalar(
        select(OrchestrationTrackerPublication.id)
        .where(
            OrchestrationTrackerPublication.org_id == org_id,
            OrchestrationTrackerPublication.flow_id == flow_id,
            OrchestrationTrackerPublication.completed_at.is_(None),
        )
        .limit(1)
    ):
        raise LegacyMigrationError("pending_publication_reconciliation_required")
    repository = OrchestrationRepository(session)
    if request.product_id is not None:
        if flow.product_id is not None or (plan and plan.plan_document.get("product_id") is not None):
            raise LegacyMigrationError("product_already_bound")
        existing = await session.scalar(
            select(OrchestrationLegacyMigration).where(
                OrchestrationLegacyMigration.org_id == org_id,
                OrchestrationLegacyMigration.flow_id == flow_id,
                OrchestrationLegacyMigration.product_id.is_not(None),
            )
        )
        if existing is not None:
            raise LegacyMigrationError("product_already_attributed")
        await require_accessible_product(session, product_id=request.product_id, decision=actor)
        decision = await repository.append_decision(
            org_id=org_id,
            flow_id=flow_id,
            kind=DecisionKind.PRODUCT_ATTRIBUTED.value,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind=ActorKind.HUMAN.value,
            reason=f"Legacy Product attribution: {request.product_id}; {request.reason}",
        )
        result_version, node_id = request.expected_plan_version, None
    else:
        if plan is None:
            raise LegacyMigrationError("accepted_plan_required")
        target = request.repository
        node = await session.scalar(
            select(OrchestrationNode)
            .where(
                OrchestrationNode.org_id == org_id,
                OrchestrationNode.flow_id == flow_id,
                OrchestrationNode.id == target.node_id,
            )
            .with_for_update()
        )
        if node is None or node.kind != "story" or node.state not in (NodeState.PENDING.value, NodeState.READY.value) or node.attempts:
            raise LegacyMigrationError("untouched_story_required")
        accepted_nodes = [
            item
            for item in plan.plan_document.get("nodes", [])
            if isinstance(item, dict) and item.get("address") == f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
        ]
        if len(accepted_nodes) != 1 or any(
            accepted_nodes[0].get(field) is not None for field in ("repository_identity", "provider_repository_id", "repository_connection")
        ):
            raise LegacyMigrationError("binding_not_legacy_unbound")
        started, _ = await binding_reservations(session, flow=flow, nodes=[node], document=plan.plan_document)
        if node.id in started:
            raise LegacyMigrationError("assignment_must_be_preserved")
        if not node.issue_ref or not str(node.issue_ref).lstrip("#").isdigit():
            raise LegacyMigrationError("verified_issue_required")
        verified = await resolve_story_repository(
            session,
            tenant_id=org_id,
            name=target.name,
            issue=int(str(node.issue_ref).lstrip("#")),
            operation="accept",
            repository_id=target.repository_id,
            installation_id=target.installation_id,
        )
        if (node.provider_repository_id is not None and node.provider_repository_id != verified.repository_id) or (
            node.repository_identity is not None and node.repository_identity.casefold() != verified.full_name.casefold()
        ):
            raise LegacyMigrationError("projection_conflicts_with_verified_target")
        proposal = LoopProposal.model_validate(plan.plan_document)
        proposed = next(item for item in proposal.nodes if item.address == accepted_nodes[0]["address"])
        proposed.repository_identity = verified.full_name
        proposed.provider_repository_id = verified.repository_id
        proposed.repository_connection = RepositoryConnection(provider="github", installation_id=verified.installation_id)
        amended = await amend_plan(
            session,
            flow.id,
            proposal,
            AmendmentContext(
                org_id=org_id,
                actor_id=actor.actor_id,
                actor_role=actor.actor_role,
                username=actor.username,
                reason=f"Legacy binding migration: {request.reason}",
            ),
        )
        if amended.already_amended:
            raise LegacyMigrationError("binding_already_amended")
        result_version, node_id = amended.plan_version, node.id
        decision = await session.get(OrchestrationDecision, amended.decision_id)
    receipt = OrchestrationLegacyMigration(
        org_id=org_id,
        flow_id=flow_id,
        request_id=request.request_id,
        request_hash=request_hash,
        product_id=request.product_id,
        node_id=node_id,
        base_plan_version=request.expected_plan_version,
        base_plan_hash=request.expected_plan_hash,
        result_plan_version=result_version,
        decision_id=decision.id,
    )
    session.add(receipt)
    await session.flush()
    return LegacyMigrationResult(receipt.id, result_version, False)
