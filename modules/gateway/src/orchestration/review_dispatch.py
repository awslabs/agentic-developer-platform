"""Publish review inputs from protected state into the reserved child envelope."""

import re

from src.agentauth.policy import PolicyError
from src.orchestration.execution_state import ExecutionStatus, OutcomeKind
from src.orchestration.execution_store import load_execution
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.review_evidence import ReviewEvidenceError
from src.orchestration.review_ingest import resolve_review_context


async def review_dispatch_expectation(session, *, grant, node, attempt, reviewer_run_id, installation_id, repo):
    """Called under graph dispatch's flow/node locks, before reservation and send.

    The observer resolves the same facts again on upload. These inputs let the
    worker produce a correctly bound document; they confer no authority themselves.
    Legacy flows without an accepted policy keep their existing envelope.
    """
    inputs = await load_in_force_policy(session, org_id=grant.tenant_id, flow_id=grant.flow_id)
    if inputs.refusal is not None:
        raise PolicyError(409, f"review dispatch refused: {inputs.refusal.reason.value}")
    if inputs.policy is None:
        return None
    try:
        context = await resolve_review_context(
            session,
            org_id=grant.tenant_id,
            node_id=node.id,
            attempt=attempt,
            reviewer_run_id=reviewer_run_id,
            installation_id=installation_id,
        )
    except ReviewEvidenceError as error:
        raise PolicyError(409, f"review dispatch refused: {error.code.value}") from None
    identity = context.identity
    if context.flow_id != grant.flow_id or context.binding.repo != repo or context.binding.installation_id != installation_id:
        raise PolicyError(409, "review dispatch refused: repository_mismatch")
    if identity.accepted_plan_version != inputs.plan_version:
        raise PolicyError(409, "review dispatch refused: stale_policy_version")
    outcome = await load_execution(session, identity=identity)
    if (
        outcome is None
        or outcome.kind is not OutcomeKind.APPLIED
        or outcome.record is None
        or outcome.record.status
        not in {
            ExecutionStatus.RUNNABLE,
            ExecutionStatus.AWAITING_EXTERNAL,
        }
    ):
        raise PolicyError(409, "review dispatch refused: execution_not_active")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", context.actual_head_sha):
        raise PolicyError(409, "review dispatch refused: head_unverified")
    return {
        "org_id": identity.org_id,
        "flow_id": context.flow_id,
        "node_id": identity.node_id,
        "cycle": identity.cycle,
        "accepted_plan_version": identity.accepted_plan_version,
        "claim_id": identity.claim_id,
        "claim_generation": identity.claim_generation,
        "author_run_id": context.author_run_id,
        "execution_id": context.execution_id,
        "expected_head_sha": context.actual_head_sha,
        "repo": context.binding.repo,
        "pr_number": context.binding.pr_number,
        "provider_repository_id": context.binding.provider_repository_id,
        "provider_pr_node_id": context.binding.provider_pr_node_id,
    }
