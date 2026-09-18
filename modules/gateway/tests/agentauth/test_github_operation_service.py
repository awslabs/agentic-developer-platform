"""Mediated GitHub operations against real accepted-policy and claim rows (#5223).

The contract's own properties are proven in `test_github_operations.py`. What is
proven here is the part that can only be wrong against real records: that the
authority an operation runs with comes from protected rows, that the human merge
gate still refuses merge on a perfectly valid develop assignment, and that a
grant, claim or acceptance changing between calls stops the NEXT effect.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.agentauth.github_operation_service import (
    authorize_operation,
    build_assignment,
    operation_permissions,
    require_current_claim,
)
from src.agentauth.github_operations import GitHubOperation, OperationRefusedError
from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.orchestration.models import (
    ClaimState,
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationWorkClaim,
)
from src.orchestration.state import NodeState
from src.shared.models.audit import AuditLog  # noqa: F401 — register before fixture creates schema
from tests.orchestration.test_policy_admission import (
    APPROVER,
    INSTALLATION_A,
    ORG_A,
    REPO,
    _fixture,
    _limits,
    _policy,
)
from tests.orchestration.test_policy_admission import (
    engine as engine_fixture,
)
from tests.orchestration.test_policy_admission import (
    healthy_policy_reservations as reservations_fixture,
)
from tests.orchestration.test_policy_admission import (
    policy_budget_initializers as initializers_fixture,
)
from tests.orchestration.test_policy_admission import (
    session as session_fixture,
)

engine = engine_fixture
healthy_policy_reservations = reservations_fixture
session = session_fixture
policy_budget_initializers = initializers_fixture

REPOSITORY_ID = 987654
# The shared fixture's node carries this issue. The work claim, the protected
# execution and the node must agree on it: `authorize_worker_credential` checks
# the claim against the node's own `issue_ref`, so a mismatch here would refuse
# for the wrong reason and hide whatever the test meant to prove.
ISSUE = 4196
POD = "pod-uid-1"
GENERATION = 1


@pytest.fixture(autouse=True)
def work_claims_enabled(monkeypatch):
    """Mediated mutations require an enforceable claim generation."""
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "true")
    monkeypatch.setenv("AGENT_AUTHORITY_ENABLED", "true")


@pytest.fixture
async def assignment(session):
    """A current, fully authorized developer assignment with merge gated to a human.

    This is the exact situation the story exists for: develop is autonomous, merge
    requires a person, and today the work is refused because no token can express
    that split.
    """
    flow, node = await _fixture(
        session,
        policy=_policy(limits=_limits(max_wall_clock_seconds=7200), human_gates=["merge"]),
        node_kwargs={"state": NodeState.RUNNING, "attempts": 1},
    )
    approval = await session.scalar(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="system:orchestration-dispatch",
            actor_role="engine",
            actor_kind="service",
            created_at=datetime.now(UTC) - timedelta(seconds=10),
        )
    )
    session.add(
        OrchestrationWorkClaim(
            org_id=ORG_A,
            provider_repository_id=REPOSITORY_ID,
            issue_number=ISSUE,
            owner_kind="engine_flow",
            owner_ref=flow.id,
            state=ClaimState.HELD.value,
            generation=GENERATION,
            active_run_id="worker",
            claim_event_id="worker",
        )
    )
    await session.flush()
    execution = {
        "invocation_id": {"S": "worker"},
        "tenant_id": {"S": ORG_A},
        "flow_id": {"S": flow.id},
        "orchestration_node_id": {"S": node.id},
        "orchestration_node_attempt": {"N": "1"},
        "persona": {"S": "developer"},
        "repo": {"S": REPO},
        "installation_id": {"N": str(INSTALLATION_A)},
        "provider_repository_id": {"N": str(REPOSITORY_ID)},
        "issue_number": {"N": str(ISSUE)},
        "default_branch": {"S": "main"},
    }
    grant = DelegatedGrant(
        grant_id="grant:worker:1",
        tenant_id=ORG_A,
        principal="worker#1",
        authority=AuthorityReference("gate_decision", approval.id, APPROVER, ORG_A),
        allowed_actions=frozenset(),
        flow_id=flow.id,
        repo_scope=frozenset({REPO}),
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    return SimpleNamespace(flow=flow, node=node, execution=execution, grant=grant)


async def authorize(session, assignment, operation=GitHubOperation.PUBLISH_COMMIT, **overrides):
    kwargs = {
        "execution": assignment.execution,
        "grant": assignment.grant,
        "operation": operation,
        "workload_binding": POD,
        "claim_generation": GENERATION,
    }
    kwargs.update(overrides)
    return await authorize_operation(session, **kwargs)


# --- The refusal this story unblocks, without weakening it -------------------


async def test_commit_is_authorized_while_merge_stays_human_only(session, assignment):
    """The whole point: the SAME assignment can publish a commit and cannot merge.

    A `contents: write` token could not express this, which is why the work was
    refused. Mediation expresses it because the capability never leaves the
    gateway and merge is a separately authorized operation.
    """
    authorized = await authorize(session, assignment, GitHubOperation.PUBLISH_COMMIT)
    assert authorized.assignment.branch == f"agent/issue-{ISSUE}"
    assert authorized.assignment.repository_id == REPOSITORY_ID

    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment, GitHubOperation.MERGE_PULL_REQUEST)


async def test_installation_token_permissions_are_narrowed_per_operation(session, assignment):
    """A review operation never receives the contents-write that implies merge."""
    assert operation_permissions(GitHubOperation.PUBLISH_REVIEW)["contents"] == "read"
    assert operation_permissions(GitHubOperation.PUBLISH_COMMIT)["contents"] == "write"
    assert operation_permissions(GitHubOperation.READ_REPOSITORY)["pull_requests"] == "read"


async def test_merge_is_authorized_only_when_merge_is_autonomous(session, assignment):
    """Ungating merge admits the merge operation — the gate, not the path, decides."""
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    document = dict(plan.plan_document)
    document["execution_policy"] = dict(document["execution_policy"], human_gates=[])
    plan.plan_document = document
    await session.flush()
    authorized = await authorize(session, assignment, GitHubOperation.MERGE_PULL_REQUEST)
    assert authorized.operation is GitHubOperation.MERGE_PULL_REQUEST


# --- Authority is derived from protected records ----------------------------


async def test_asserted_repository_and_branch_cannot_override_the_record(session, assignment):
    """A request field is an assertion. Disagreeing with the record is a refusal."""
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment, asserted_repository="attacker/repo")
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment, asserted_branch="main")

    authorized = await authorize(session, assignment, asserted_repository=REPO, asserted_branch=f"agent/issue-{ISSUE}")
    assert authorized.assignment.repository == REPO


async def test_branch_is_derived_even_when_nothing_asserts_one(session, assignment):
    """There is no parameter by which a caller influences the target branch."""
    authorized = await authorize(session, assignment)
    assert authorized.assignment.branch == f"agent/issue-{ISSUE}"
    assert authorized.assignment.branch != authorized.assignment.default_branch


async def test_missing_immutable_repository_id_is_refused(session, assignment):
    """Authorizing an `owner/name` string would survive a rename into a squatter."""
    decision = SimpleNamespace(provider_repository_id=None, not_after=datetime.now(UTC) + timedelta(minutes=5), plan_version=1)
    with pytest.raises(OperationRefusedError):
        build_assignment(
            execution=assignment.execution,
            decision=decision,
            workload_binding=POD,
            claim_generation=GENERATION,
            now=datetime.now(UTC),
        )


async def test_boolean_repository_id_is_not_an_identity(session, assignment):
    """`isinstance(True, int)` is True in Python, so booleans are excluded outright."""
    decision = SimpleNamespace(provider_repository_id=True, not_after=datetime.now(UTC) + timedelta(minutes=5), plan_version=1)
    with pytest.raises(OperationRefusedError):
        build_assignment(
            execution=assignment.execution,
            decision=decision,
            workload_binding=POD,
            claim_generation=GENERATION,
            now=datetime.now(UTC),
        )


@pytest.mark.parametrize("field", ["installation_id", "issue_number", "orchestration_node_id", "repo"])
async def test_incomplete_protected_assignment_is_refused(session, assignment, field):
    del assignment.execution[field]
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


@pytest.mark.parametrize("field,value", [("repo", "other/repo"), ("tenant_id", "other-tenant"), ("orchestration_node_id", "other-node")])
async def test_forged_execution_record_is_refused(session, assignment, field, value):
    assignment.execution[field] = {"S": value}
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


# --- Re-authorization before every effect -----------------------------------


async def test_revoked_grant_blocks_the_next_operation(session, assignment):
    """The first call succeeding does not carry the second. Revocation stops the
    next effect; an already-completed provider action is not undone here."""
    assert await authorize(session, assignment)
    assignment.grant = replace(assignment.grant, revoked=True)
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


async def test_expired_grant_blocks_the_next_operation(session, assignment):
    assignment.grant = replace(assignment.grant, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


async def test_superseded_plan_does_not_silently_authorize_an_old_worker(session, assignment):
    plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == assignment.flow.id))
    plan.superseded_at = datetime.now(UTC)
    await session.flush()
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


async def test_exhausted_wall_clock_blocks_the_next_operation(session, assignment, monkeypatch):
    later = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr("src.orchestration.runtime_policy.utcnow", lambda: later)
    monkeypatch.setattr("src.orchestration.policy_admission.utcnow", lambda: later)
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


async def test_reviewer_cannot_reuse_a_develop_assignment(session, assignment):
    assignment.execution["persona"] = {"S": "reviewer"}
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


# --- Work ownership fences every mutation ----------------------------------


async def test_released_claim_blocks_a_mutation(session, assignment):
    claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.claim_event_id == "worker"))
    claim.state = ClaimState.RELEASED.value
    await session.flush()
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


async def test_advanced_generation_blocks_a_stale_worker(session, assignment):
    """Work that changed hands leaves the previous holder unable to write."""
    claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.claim_event_id == "worker"))
    claim.generation = GENERATION + 1
    await session.flush()
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment, claim_generation=GENERATION)


async def test_claim_owned_by_another_run_blocks_a_mutation(session, assignment):
    claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.claim_event_id == "worker"))
    claim.active_run_id = "other-run"
    await session.flush()
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


async def test_missing_claim_blocks_a_mutation(session, assignment):
    with pytest.raises(OperationRefusedError):
        await require_current_claim(session, org_id=ORG_A, invocation_id="no-such-run", claim_generation=GENERATION)


async def test_unenforceable_claims_refuse_rather_than_pass(session, assignment, monkeypatch):
    """With claims disabled the generation cannot be checked. "Cannot check" is
    not "checked and fine" — a mediated write needs positive ownership."""
    monkeypatch.setenv("ADP_WORK_CLAIMS_ENABLED", "false")
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment)


async def test_a_released_claim_blocks_reads_too(session, assignment):
    """Losing the lane refuses even a read, because the underlying assignment check
    already requires a held claim.

    This is deliberately NOT relaxed for reads. `authorize_worker_credential`
    treats a claim held by this flow and run as part of what makes the assignment
    current, so carving out an exception for reads would weaken an existing
    control to add a convenience. A worker that no longer owns the work has no
    business reading the repository as that assignment either.
    """
    claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.claim_event_id == "worker"))
    claim.state = ClaimState.RELEASED.value
    await session.flush()
    with pytest.raises(OperationRefusedError):
        await authorize(session, assignment, GitHubOperation.READ_REPOSITORY)
