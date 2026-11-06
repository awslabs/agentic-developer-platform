"""An unfinished protected PR keeps its lane even when handoff reporting failed."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_runner import _load_due
from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from src.orchestration.execution_store import advance_execution, load_execution
from src.orchestration.models import ClaimState, OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.pr_bindings import PullRequestIdentity, register_binding, resolve_registration_target
from src.orchestration.review_cycle import PHASES
from src.orchestration.state import ActorKind
from src.orchestration.work_admission import maintain_worker_claim, recover_exited_claims
from src.orchestration.work_claims import ReleaseReason
from tests.orchestration import test_work_claims as fixtures
from tests.orchestration.test_work_admission import ORG, handed_off_lane, select_execution

engine = fixtures.engine
session = fixtures.session
session_factory = fixtures.session_factory


async def bound_lane(db, *, marked=True, run="developer", issue=8065):
    claim_id, _, identity, _ = await handed_off_lane(db, run=run, issue=issue, commit=False)
    run = attempt_run_id(identity.node_id, identity.cycle)
    claim = await db.get(OrchestrationWorkClaim, claim_id)
    claim.active_run_id = claim.claim_event_id = run
    node = await db.get(OrchestrationNode, identity.node_id)
    node.state, node.attempts = "running", identity.cycle
    decision = OrchestrationDecision(
        org_id=ORG,
        flow_id=node.flow_id,
        node_id=node.id,
        kind="node_dispatched",
        actor_kind="service",
        actor_id="system:orchestration-dispatch",
        actor_role="engine",
        reason=json.dumps(
            {
                "run_id": run,
                "attempt": identity.cycle,
                "repo": "org/repo",
                "issue": issue,
                "provider_repository_id": 1234,
                "installation_id": 42,
                "pr_binding_required": True,
                "handoff_required": marked,
            }
        ),
    )
    db.add(decision)
    await db.flush()
    target = await resolve_registration_target(db, run_id=run, expected_org_id=ORG)
    binding, _ = await register_binding(
        db,
        target=target,
        pr=PullRequestIdentity(1234, f"PR_{issue}", "org/repo", issue, "a" * 40),
        actor_id=run,
        actor_kind=ActorKind.SERVICE,
    )
    return claim_id, run, identity, binding


async def test_terminal_success_keeps_bound_review_due_without_inventing_a_handoff(session, session_factory):
    claim_id, run, identity, _ = await bound_lane(session)
    await maintain_worker_claim(session, org_id=ORG, invocation_id=run, terminal=True)
    claim = await session.get(OrchestrationWorkClaim, claim_id)
    assert (claim.state, claim.generation, claim.active_run_id) == (ClaimState.HELD.value, 1, run)
    current = await load_execution(session, identity=identity)
    assert current.kind is OutcomeKind.APPLIED and current.record.handoff_receipt_ref is None
    assert current.record.phase is ExecutionPhase.ADMITTED
    await session.commit()
    due = await _load_due(session_factory, now=datetime.now(UTC), limit=10, phases=frozenset(PHASES))
    assert [row.id for row in due] == [current.record.id]


async def test_completed_worker_recovery_preserves_bound_review_and_keeps_sweeping(session):
    claim_id, run, identity, _ = await bound_lane(session)
    other, _, _, _ = await bound_lane(session, marked=False, run="legacy", issue=8066)
    store = SimpleNamespace(_read=Mock(return_value={"tenant_id": {"S": ORG}, "status": {"S": "completed"}, "terminal_outcome": {"S": "complete"}}))
    report = await recover_exited_claims(session, store=store, workloads=None)
    assert report.released == 1
    assert (await session.get(OrchestrationWorkClaim, claim_id)).state == ClaimState.HELD.value
    assert (await session.get(OrchestrationWorkClaim, other)).state == ClaimState.RELEASED.value
    assert (await session.scalar(select_execution(identity))).handoff_receipt_ref is None


@pytest.mark.parametrize("status", [ExecutionStatus.CONCLUDED, ExecutionStatus.SUPERSEDED])
async def test_terminal_execution_releases_bound_claim(session, status):
    claim_id, run, identity, _ = await bound_lane(session)
    current = await load_execution(session, identity=identity)
    outcome = await advance_execution(
        session,
        identity=identity,
        advance=PhaseAdvance(phase=ExecutionPhase.CONCLUDED, status=status, expected_revision=current.record.revision),
    )
    assert outcome.kind is OutcomeKind.APPLIED
    await maintain_worker_claim(session, org_id=ORG, invocation_id=run, terminal=True)
    assert (await session.get(OrchestrationWorkClaim, claim_id)).state == ClaimState.RELEASED.value


@pytest.mark.parametrize("change", ["unmarked", "generation", "attempt", "superseded_node", "scope", "review_artifact", "superseded_binding"])
async def test_legacy_or_stale_binding_does_not_retain_claim(session, change):
    claim_id, run, identity, binding = await bound_lane(session, marked=change != "unmarked")
    claim = await session.get(OrchestrationWorkClaim, claim_id)
    node = await session.get(OrchestrationNode, identity.node_id)
    if change == "generation":
        claim.generation += 1
    elif change == "attempt":
        node.attempts += 1
    elif change == "superseded_node":
        node.state = "superseded"
    elif change == "scope":
        node.title = "replacement definition"
    elif change == "review_artifact":
        binding.role = "reviewer_artifact"
    elif change == "superseded_binding":
        binding.state = "superseded"
    await session.flush()
    await maintain_worker_claim(session, org_id=ORG, invocation_id=run, terminal=True)
    assert claim.state == ClaimState.RELEASED.value


async def test_failed_developer_without_handoff_retains_existing_failure_recovery(session):
    claim_id, run, _, _ = await bound_lane(session)
    await maintain_worker_claim(session, org_id=ORG, invocation_id=run, terminal=True, reason=ReleaseReason.FAILED)
    assert (await session.get(OrchestrationWorkClaim, claim_id)).state == ClaimState.RELEASED.value
