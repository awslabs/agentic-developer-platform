"""Read-only inventory of stored orchestration repository provenance."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationLegacyMigration,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationTrackerPublication,
)


def _binding(identity, repository_id, connection):
    if (
        isinstance(identity, str)
        and "/" in identity
        and type(repository_id) is int
        and repository_id > 0
        and isinstance(connection, dict)
        and connection.get("provider") == "github"
        and type(connection.get("installation_id")) is int
        and connection["installation_id"] > 0
    ):
        return (identity, repository_id, connection["installation_id"])
    return None


async def inventory_legacy_bindings(session: AsyncSession, *, org_id: str, flow_id: str | None = None) -> list[dict]:
    """Return classifications, never mutate storage or derive identity from global config."""
    flows = (
        await session.scalars(
            select(OrchestrationFlow)
            .where(OrchestrationFlow.org_id == org_id, *((OrchestrationFlow.id == flow_id,) if flow_id else ()))
            .order_by(OrchestrationFlow.id)
        )
    ).all()
    results = []
    for flow in flows:
        plans = (
            await session.scalars(
                select(OrchestrationAcceptedPlan).where(
                    OrchestrationAcceptedPlan.org_id == org_id,
                    OrchestrationAcceptedPlan.flow_id == flow.id,
                    OrchestrationAcceptedPlan.superseded_at.is_(None),
                )
            )
        ).all()
        plan = plans[0] if len(plans) == 1 else None
        document = plan.plan_document if plan and isinstance(plan.plan_document, dict) else {}
        accepted_nodes = (
            {item["address"]: item for item in document.get("nodes", []) if isinstance(item, dict) and isinstance(item.get("address"), str)}
            if isinstance(document.get("nodes"), list)
            else {}
        )
        attribution = await session.scalar(
            select(OrchestrationLegacyMigration).where(
                OrchestrationLegacyMigration.org_id == org_id,
                OrchestrationLegacyMigration.flow_id == flow.id,
                OrchestrationLegacyMigration.product_id.is_not(None),
            )
        )
        nodes = (
            await session.scalars(
                select(OrchestrationNode)
                .where(OrchestrationNode.org_id == org_id, OrchestrationNode.flow_id == flow.id)
                .order_by(OrchestrationNode.id)
            )
        ).all()
        bindings = (
            await session.scalars(
                select(OrchestrationPullRequestBinding).where(
                    OrchestrationPullRequestBinding.org_id == org_id,
                    OrchestrationPullRequestBinding.flow_id == flow.id,
                )
            )
        ).all()
        by_node = {}
        for binding in bindings:
            by_node.setdefault(binding.node_id, []).append(binding)
        pending = (
            await session.execute(
                select(OrchestrationExecution.node_id, OrchestrationAction.id)
                .join(OrchestrationAction, OrchestrationAction.execution_id == OrchestrationExecution.id)
                .where(
                    OrchestrationExecution.org_id == org_id,
                    OrchestrationExecution.flow_id == flow.id,
                    OrchestrationAction.org_id == org_id,
                    OrchestrationAction.status.in_(("prepared", "dispatched", "unknown")),
                )
            )
        ).all()
        pending_by_node = {}
        for node_id, action_id in pending:
            pending_by_node.setdefault(node_id, []).append(action_id)
        pending_publication_ids = sorted(
            await session.scalars(
                select(OrchestrationTrackerPublication.id).where(
                    OrchestrationTrackerPublication.org_id == org_id,
                    OrchestrationTrackerPublication.flow_id == flow.id,
                    OrchestrationTrackerPublication.completed_at.is_(None),
                )
            )
        )
        entries = []
        for node in nodes:
            address = f"{flow.slug}/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"
            accepted = accepted_nodes.get(address, {})
            accepted_binding = _binding(
                accepted.get("repository_identity"), accepted.get("provider_repository_id"), accepted.get("repository_connection")
            )
            projected_binding = _binding(node.repository_identity, node.provider_repository_id, node.repository_connection)
            prior = by_node.get(node.id, [])
            has_started = node.attempts > 0 or bool(prior)
            if len(plans) > 1 or (plan and address not in accepted_nodes):
                classification, reason = "legacy_binding_unresolved", "accepted_plan_conflict"
            elif accepted_binding and (projected_binding is None or projected_binding == accepted_binding):
                classification, reason = "explicit_accepted_binding", "accepted_plan_binding"
            elif accepted_binding:
                classification, reason = "legacy_binding_unresolved", "projection_conflicts_with_accepted_plan"
            elif has_started:
                consistent = (
                    bool(prior)
                    and all(
                        item.provider_repository_id == prior[0].provider_repository_id
                        and item.installation_id == prior[0].installation_id
                        and item.repo == prior[0].repo
                        for item in prior
                    )
                    and (projected_binding is None or projected_binding == (prior[0].repo, prior[0].provider_repository_id, prior[0].installation_id))
                )
                classification = "started_provenance_requires_verification" if consistent else "legacy_binding_unresolved"
                reason = "protected_assignment_requires_provider_read" if consistent else "started_provenance_conflict"
            elif node.state in ("pending", "ready") and node.attempts == 0 and projected_binding:
                classification, reason = "untouched_target_requires_verification", "projection_is_not_accepted_authority"
            else:
                classification, reason = "legacy_binding_unresolved", "no_trustworthy_repository_provenance"
            entries.append(
                {
                    "node_id": node.id,
                    "address": address,
                    "classification": classification,
                    "reason": reason,
                    "pending_action_ids": sorted(pending_by_node.get(node.id, [])),
                    "action": "reconcile_pending_effects"
                    if pending_by_node.get(node.id)
                    else ("retain_assignment" if has_started else "verify_target_or_hold"),
                }
            )
        results.append(
            {
                "org_id": org_id,
                "flow_id": flow.id,
                "product_missing": flow.product_id is None,
                "attributed_product_id": attribution.product_id if attribution else None,
                "accepted_plan_version": plan.version if plan else None,
                "accepted_plan_hash": plan.plan_hash if plan else None,
                "pending_publication_ids": pending_publication_ids,
                "nodes": entries,
            }
        )
    return results
