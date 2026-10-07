"""Read reviewer delivery ownership from the committed dispatch, for either transport."""

from __future__ import annotations

import json

from sqlalchemy import select

from .models import OrchestrationDecision
from .run_reports import OrchestrationRunReport


async def review_assignment(session, *, org_id, node_id, run_id):
    report = await session.get(OrchestrationRunReport, run_id)
    if report is not None and report.org_id == org_id and report.node_id == node_id:
        return report.dispatch_metadata
    rows = list(
        (
            await session.scalars(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.org_id == org_id,
                    OrchestrationDecision.node_id == node_id,
                    OrchestrationDecision.kind == "agent_dispatched",
                    OrchestrationDecision.actor_id == "system:review-cycle",
                    OrchestrationDecision.actor_kind == "service",
                    OrchestrationDecision.reason.contains(run_id),
                )
                .limit(2)
            )
        ).all()
    )
    if len(rows) != 1:
        return None
    saved = json.loads(rows[0].reason)
    envelope = saved.get("envelope") or {}
    if saved.get("run_id") != run_id or envelope.get("message_id") != run_id or envelope.get("persona") != "agent-codex-reviewer":
        return None
    return envelope


async def reviewer_owns_delivery(session, *, org_id, node_id, run_id):
    envelope = await review_assignment(session, org_id=org_id, node_id=node_id, run_id=run_id)
    return bool(envelope and (envelope.get("review_cycle_input") or {}).get("reviewer_owned_delivery") is True)


async def require_reviewer_merge(session, *, org_id, node_id, run_id, provider=None):
    """A delivery assignment cannot report success merely for producing a review."""
    import httpx

    from src.agentauth.github_provider import ProviderUnavailableError

    from .merge_provider import MergeProvider
    from .models import OrchestrationNode
    from .pr_bindings import active_binding_for_node, binding_scope_matches
    from .review_cycle import CycleBlockedError
    from .run_reports import RunReportError

    if not await reviewer_owns_delivery(session, org_id=org_id, node_id=node_id, run_id=run_id):
        return
    node = await session.get(OrchestrationNode, node_id)
    if node is None or node.org_id != org_id:
        raise RunReportError("reviewer_merge_binding_changed")
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node_id, attempt=node.attempts)
    if binding is None or not binding_scope_matches(binding, node):
        raise RunReportError("reviewer_merge_binding_changed")
    try:
        state = await (provider or MergeProvider()).read(binding)
    except (ProviderUnavailableError, httpx.HTTPError, CycleBlockedError):
        raise RunReportError("reviewer_merge_unverifiable", retryable=True) from None
    if not state.merged or not state.merge_sha:
        raise RunReportError("reviewer_merge_not_delivered")
