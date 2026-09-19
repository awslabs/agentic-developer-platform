"""A merged PR plus a clean worker exit is not completion without a receipt (#5144).

`test_story_reconciliation.py` covers which *evidence path* a finished story takes.
This file covers the additional condition #5144 imposes on the passing path: a run
dispatched under the handoff contract must also have committed a durable continuation
receipt, because merge evidence says nothing about the review, deployment and
evaluation a worker's green exit can leave outstanding.

The assertion that matters throughout is the **durable outcome**, not the absence of a
receipt. "No receipt row exists" would also be true of code that silently dropped the
work, which is worse than the defect being fixed — so every missing-receipt test here
asserts the node is still held and still due, and the acceptance test asserts it
passes only once a receipt is genuinely durable.

The boundary is the same shape as #5301's and is deliberate:

- an **unmarked** dispatch is unchanged, byte for byte, and pays no extra read;
- a **marked** dispatch with no attributable receipt **holds**, with a stated reason.

"No marker, therefore require a receipt" would hold every in-flight legacy run the
moment this deploys. "No receipt, therefore complete" is the defect restated. Reading
the marker off the run's own dispatch record is what lets both behaviours coexist.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration.execution_state import BlockCode, ExecutionIdentity, ExecutionStatus, OutcomeKind, PhaseAdvance
from src.orchestration.execution_store import advance_execution, create_execution, load_execution
from src.orchestration.handoff import commit_handoff, handoff_required, missing_receipt_hold, outstanding_block
from src.orchestration.models import (
    ActorKind,
    ClaimState,
    DecisionKind,
    NodeState,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationWorkClaim,
)
from src.orchestration.results import _story_evidence, observe_results
from src.orchestration.work_claims import OwnerKind
from tests.orchestration import test_story_reconciliation as fixtures
from tests.orchestration.test_story_reconciliation import (
    ISSUE,
    PR_URL,
    REPO_ID,
    StubSource,
    _bind,
    _green,
    _story,
)

engine = fixtures.engine
session = fixtures.session
_installation = fixtures._installation

PLAN_VERSION = 4
CLAIM_ID = "claim-5144-recon"
INSTALLATION = fixtures.INSTALLATION


async def _ledger(session, node, *, generation: int = 1) -> ExecutionIdentity:
    """Give the node the accepted plan, held claim and execution row a receipt needs."""
    session.add(
        OrchestrationAcceptedPlan(
            org_id=node.org_id,
            flow_id=node.flow_id,
            version=PLAN_VERSION,
            plan_document={},
            plan_hash="plan-5144",
        )
    )
    session.add(
        OrchestrationWorkClaim(
            id=CLAIM_ID,
            org_id=node.org_id,
            provider_repository_id=REPO_ID,
            issue_number=ISSUE,
            owner_kind=OwnerKind.ENGINE_FLOW.value,
            owner_ref=node.flow_id,
            state=ClaimState.HELD.value,
            generation=generation,
        )
    )
    await session.flush()
    identity = ExecutionIdentity(
        org_id=node.org_id,
        node_id=node.id,
        cycle=1,
        accepted_plan_version=PLAN_VERSION,
        claim_id=CLAIM_ID,
        claim_generation=generation,
    )
    outcome = await create_execution(session, identity=identity, flow_id=node.flow_id)
    assert outcome.kind is OutcomeKind.APPLIED
    return identity


def _marked(dispatch: dict) -> dict:
    """The same dispatch record, stamped as owing a handoff receipt."""
    return {**dispatch, "handoff_required": True}


async def _marked_story(session):
    """A story whose PERSISTED dispatch record is marked, for the `observe_results` path.

    `_marked` only decorates a dict, which is enough for `_story_evidence` (it is
    handed the record directly). `observe_results` re-reads the dispatch from its
    `NODE_DISPATCHED` decision, so the marker has to be durable there or the whole
    handoff contract is invisible to the sweep.

    Appended as a NEW decision rather than by rewriting the seeded one:
    `orchestration_decisions` is append-only and enforces it at the flush boundary.
    `observe_results` reads the newest `NODE_DISPATCHED` row, so this is also what a
    real re-dispatch of the same attempt would look like.
    """
    node, dispatch = await _story(session, binding_marker=True)
    marked = _marked(dispatch)
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="engine",
            actor_role="service",
            actor_kind=ActorKind.SERVICE.value,
            reason=json.dumps(marked),
        )
    )
    await session.flush()
    return node, marked


def _patch_results_installation(monkeypatch) -> None:
    """`results` resolves the installation itself, and the autouse fixture misses it.

    `test_story_reconciliation._installation` patches `pr_bindings` only, which is
    enough for tests that call `_story_evidence` directly. The `observe_results` path
    resolves it in `results`, so without this the sweep raises "GitHub installation is
    unresolved" and the test would pass for the wrong reason.
    """

    async def _resolve(_session, *, org_id):
        return INSTALLATION

    monkeypatch.setattr("src.orchestration.results.resolve_installation_id", _resolve)


class _RunStore:
    """A worker run that reported a clean `complete` exit — the story's whole premise."""

    def __init__(self, node) -> None:
        self.node_id, self.org_id, self.attempt = node.id, node.org_id, node.attempts

    def get(self, *_args):
        return {"tenant_id": self.org_id, "engine_node_id": self.node_id, "engine_attempt": self.attempt, "status": "complete"}


async def _observations(session, node) -> list[dict]:
    """The structured `RESULT_OBSERVED` payloads, oldest first.

    `RESULT_CHECKED` is deliberately excluded: it is the sweep's own cursor row and
    carries plain prose, not JSON.
    """
    rows = (
        (
            await session.execute(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.node_id == node.id,
                    OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value,
                )
                .order_by(OrchestrationDecision.created_at, OrchestrationDecision.id)
            )
        )
        .scalars()
        .all()
    )
    return [json.loads(row.reason or "{}") for row in rows]


# ---------------------------------------------------------------------------
# The marker decides whether the receipt is required at all
# ---------------------------------------------------------------------------


def test_marker_absent_means_no_receipt_is_required():
    assert handoff_required({"run_id": "orch:x"}) is False
    assert handoff_required({"handoff_required": True}) is True
    assert handoff_required({"handoff_required": False}) is False


# ---------------------------------------------------------------------------
# Missing receipt: the durable outcome is "still held", not "no receipt"
# ---------------------------------------------------------------------------


async def test_merged_pr_without_a_receipt_holds_the_story(session):
    """The core acceptance: merged code, clean exit, and still not complete.

    Asserted on the durable outcome — no completion url, and a hold reason an operator
    can act on. The provider WAS consulted and did report a green merge, so this is
    specifically the "delivery landed but its continuation is unaccounted for" case
    rather than a missing-evidence case.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    await _ledger(session, node)
    source = StubSource(issue_url=None, evidence=_green())

    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch=_marked(dispatch),
        source=source,
        installation_id=INSTALLATION,
    )

    assert url is None
    assert hold == missing_receipt_hold()
    # Provider truth was green; the hold is about the continuation, not the merge.
    assert source.bound_pr_calls
    # And it never fell back to asking about the issue.
    assert source.merged_story_calls == 0


async def test_the_hold_reason_states_the_outstanding_work(session):
    """A hold an operator cannot act on is how #5301's U11 story waited forever."""
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    await _ledger(session, node)

    _, hold = await _story_evidence(
        session,
        node=node,
        dispatch=_marked(dispatch),
        source=StubSource(evidence=_green()),
        installation_id=INSTALLATION,
    )

    assert "continuation receipt" in hold
    assert "stays due" in hold
    assert "complete" in hold


async def test_marked_dispatch_with_no_execution_row_fails_closed(session):
    """Unverifiable authority holds rather than passing.

    A dispatch marked as owing a receipt but carrying no execution row cannot be
    verified either way. Fail closed: hold. Passing would let the marker become a
    no-op for exactly the runs whose ledger state is broken.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    # Deliberately no `_ledger` call.

    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch=_marked(dispatch),
        source=StubSource(evidence=_green()),
        installation_id=INSTALLATION,
    )

    assert url is None
    assert hold == missing_receipt_hold()


async def test_receipt_from_a_superseded_generation_does_not_pass_the_story(session):
    """Attribution, not presence: another owner's receipt is not this attempt's.

    A committed receipt whose generation has since been superseded must read as absent,
    because it is evidence about ownership that no longer holds the lane.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    result = await commit_handoff(session, identity=identity, now=datetime.now(UTC))
    assert result.accepted is True
    # Ownership moves on after the receipt was committed.
    claim = await session.get(OrchestrationWorkClaim, CLAIM_ID)
    claim.generation = 2
    await session.flush()

    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch=_marked(dispatch),
        source=StubSource(evidence=_green()),
        installation_id=INSTALLATION,
    )

    assert url is None
    assert hold == missing_receipt_hold()


# ---------------------------------------------------------------------------
# With a receipt: the story may pass, and the receipt is recorded as evidence
# ---------------------------------------------------------------------------


async def test_merged_pr_with_a_durable_receipt_completes(session):
    """The positive case, so the hold above is a real gate and not a permanent block.

    Without this, a fence that simply refused everything would pass every test in the
    section above.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    result = await commit_handoff(session, identity=identity, now=datetime.now(UTC))
    assert result.accepted is True
    observation: dict = {}

    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch=_marked(dispatch),
        source=StubSource(evidence=_green()),
        installation_id=INSTALLATION,
        observation=observation,
    )

    assert url == PR_URL
    assert hold == ""
    # Recorded on the observation, so the decision row states which receipt was relied
    # on rather than only that one existed at the time.
    assert observation["handoff_receipt_ref"] == result.receipt_ref


async def test_a_repeat_report_still_completes_on_the_same_receipt(session):
    """Idempotency reaches the completion decision, not just the write path."""
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    first = await commit_handoff(session, identity=identity, now=datetime.now(UTC))
    repeat = await commit_handoff(session, identity=identity, now=datetime.now(UTC))
    assert repeat.receipt_ref == first.receipt_ref
    observation: dict = {}

    url, _ = await _story_evidence(
        session,
        node=node,
        dispatch=_marked(dispatch),
        source=StubSource(evidence=_green()),
        installation_id=INSTALLATION,
        observation=observation,
    )

    assert url == PR_URL
    assert observation["handoff_receipt_ref"] == first.receipt_ref


async def test_a_retry_does_not_inherit_the_previous_cycles_receipt(session):
    """A new delivery cycle owes its own handoff; the old cycle's receipt is not it.

    The fail-open this pins: cycle 1 hands off, then the story is re-dispatched. If
    reconciliation resolves the receipt by anything looser than the current cycle, the
    retry finds cycle 1's receipt, reads as handed off, and completes without the new
    attempt ever committing one — the original defect, reintroduced by a retry.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    first_cycle = await _ledger(session, node)
    committed = await commit_handoff(session, identity=first_cycle, now=datetime.now(UTC))
    assert committed.accepted is True

    # A second delivery cycle, with no receipt of its own. `node.attempts` is left
    # alone deliberately: bumping it would invalidate the PR binding and the story
    # would hold for THAT reason instead, making the test pass without exercising the
    # cycle question at all.
    outcome = await create_execution(
        session,
        identity=ExecutionIdentity(
            org_id=node.org_id,
            node_id=node.id,
            cycle=first_cycle.cycle + 1,
            accepted_plan_version=PLAN_VERSION,
            claim_id=CLAIM_ID,
            claim_generation=1,
        ),
        flow_id=node.flow_id,
    )
    assert outcome.kind is OutcomeKind.APPLIED

    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch=_marked(dispatch),
        source=StubSource(evidence=_green()),
        installation_id=INSTALLATION,
    )

    # The durable outcome, not merely "no receipt found": the story stays held.
    assert url is None
    assert hold == missing_receipt_hold()


# ---------------------------------------------------------------------------
# Compatibility: an unmarked dispatch is untouched
# ---------------------------------------------------------------------------


async def test_unmarked_dispatch_completes_without_a_receipt(session):
    """A legacy dispatch keeps its prior behaviour exactly.

    This is the staged-deployment guarantee. Requiring a receipt from runs that were
    never told to produce one would hold every in-flight story the moment this deploys,
    which is an outage rather than a fix.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    await _ledger(session, node)

    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch=dispatch,
        source=StubSource(evidence=_green()),
        installation_id=INSTALLATION,
    )

    assert url == PR_URL
    assert hold == ""


async def test_unmarked_dispatch_reads_no_ledger_state(session):
    """The unmarked path pays no extra read, so it cannot fail in a new way.

    Asserted by removing the ledger entirely: if the unmarked path consulted it, this
    would hold instead of passing.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    # No `_ledger`: there is no execution row to read.

    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch=dispatch,
        source=StubSource(evidence=_green()),
        installation_id=INSTALLATION,
    )

    assert url == PR_URL
    assert hold == ""


async def test_a_marked_dispatch_that_was_never_going_to_pass_holds_for_its_own_reason(session):
    """The receipt check does not mask a prior refusal.

    An unmerged PR must still hold for *that* reason, so an operator is not sent to
    investigate a missing receipt when the actual state is an unmerged pull request.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    await _ledger(session, node)

    url, hold = await _story_evidence(
        session,
        node=node,
        dispatch=_marked(dispatch),
        source=StubSource(evidence=_green(merged=False)),
        installation_id=INSTALLATION,
    )

    assert url is None
    assert hold != missing_receipt_hold()


# ---------------------------------------------------------------------------
# Commit-time revalidation: the snapshot is not the authority (#5144 F2)
# ---------------------------------------------------------------------------
#
# Reviewer blocker F2 is that `_story_evidence` reads the receipt with NO locks held,
# and `observe_results` then takes the node lock and writes `PASSED` without asking
# again. A handover committed in that gap leaves the pass resting on a receipt
# `receipt_for` can no longer attribute — the #5144 defect restored through a race.
#
# The race itself needs two genuinely concurrent transactions and is therefore in
# `test_handoff_postgres.py`; SQLite serializes writers and `FOR UPDATE` is a no-op
# there, so nothing in THIS file is evidence about the interleaving. What these tests
# hold is the other half, which is just as easy to get wrong: that the revalidation is
# reached on the passing path, agrees with itself when nothing changed, and is skipped
# for an unmarked dispatch. A guard that always disagreed would pass every race test
# and hold every story in production.


async def test_a_marked_story_passes_through_the_commit_time_revalidation(session, monkeypatch):
    """The `observe_results` path completes when the receipt's authority is stable.

    This is the test that makes the revalidation a gate rather than a wall. It is also
    the only SQLite test that reaches the new locked read at all — mutating the guard
    to always refuse fails exactly here.
    """
    _patch_results_installation(monkeypatch)
    node, dispatch = await _marked_story(session)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    committed = await commit_handoff(session, identity=identity, now=datetime.now(UTC))
    assert committed.accepted is True

    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))

    assert report.advanced == 1
    assert node.state == NodeState.PASSED.value
    observed = await _observations(session, node)
    # The decision names the receipt it relied on, and it is the one that survived
    # revalidation rather than the one read before the lock.
    assert observed[-1]["handoff_receipt_ref"] == committed.receipt_ref


async def test_a_marked_story_with_no_receipt_never_reaches_a_pass(session, monkeypatch):
    """The sweep holds, so the revalidation is not the only thing standing between a
    clean worker exit and completion.

    `_story_evidence`'s own check already refuses this, and that is the point: the
    commit-time revalidation is a second fence for a narrower window, not a
    replacement. If it were the only one, every run whose receipt was absent from the
    start would pass the first check and rely entirely on a locked re-read.
    """
    _patch_results_installation(monkeypatch)
    node, dispatch = await _marked_story(session)
    await _bind(session, node, dispatch)
    await _ledger(session, node)

    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))

    assert report.advanced == 0
    assert node.state == NodeState.AWAITING_MERGE.value
    assert report.reasons[node.id] == missing_receipt_hold()


async def test_an_unmarked_dispatch_pays_no_revalidation_read(session, monkeypatch):
    """The legacy path must not take the new lock, or this deploy is a new stall risk.

    Asserted by removing the ledger entirely: a run that was never told to produce a
    receipt has no execution row to revalidate against, so if the unmarked path
    consulted one it would hold here instead of passing.
    """
    _patch_results_installation(monkeypatch)
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)

    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))

    assert report.advanced == 1
    assert node.state == NodeState.PASSED.value


async def _stored_handoff_block(session, node):
    decision = await session.scalar(
        select(OrchestrationDecision)
        .where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value)
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    assert decision is not None
    assert decision.rejection_reason is not None
    block = json.loads(decision.rejection_reason)
    assert block == json.loads(decision.reason)["handoff_block"]
    assert block["owner"] and block["required_input"]
    return block


async def test_missing_handoff_persists_a_bound_block_and_repeated_sweeps_are_idempotent(session, monkeypatch):
    _patch_results_installation(monkeypatch)
    node, dispatch = await _marked_story(session)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))
    assert report.errors == 0
    await session.commit()
    block = await _stored_handoff_block(session, node)
    row = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))
    await session.refresh(row)
    assert row.status == ExecutionStatus.BLOCKED.value
    assert row.block_code == block["block_code"] == BlockCode.AUTHORITY_UNVERIFIABLE.value
    assert row.block_owner == block["owner"] and row.block_required_input == block["required_input"]
    assert row.next_check_at is not None and row.handoff_receipt_ref is None
    assert block["identity"]["claim_generation"] == identity.claim_generation
    assert block["identity"]["accepted_plan_version"] == identity.accepted_plan_version
    revision = row.revision
    count = len(await _observations(session, node))
    await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))
    await session.refresh(row)
    assert row.revision == revision
    assert len(await _observations(session, node)) == count


@pytest.mark.parametrize("race", ["receipt", "generation", "plan"])
async def test_commit_time_handoff_failure_is_typed_without_writing_under_stale_authority(session, monkeypatch, race):
    from src.orchestration import results

    _patch_results_installation(monkeypatch)
    node, dispatch = await _marked_story(session)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    assert (await commit_handoff(session, identity=identity, now=datetime.now(UTC))).accepted
    execution = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))
    revision = execution.revision
    original = results._story_evidence

    async def move_authority(*args, **kwargs):
        observed = await original(*args, **kwargs)
        assert observed[0] == PR_URL
        if race == "generation":
            claim = await session.get(OrchestrationWorkClaim, CLAIM_ID)
            claim.generation += 1
        elif race == "plan":
            plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == node.flow_id))
            plan.superseded_at = datetime.now(UTC)
        else:
            execution.handoff_receipt_ref = None
        await session.flush()
        return observed

    monkeypatch.setattr(results, "_story_evidence", move_authority)
    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))
    assert report.errors == 0 and node.state == NodeState.AWAITING_MERGE.value
    await session.commit()
    block = await _stored_handoff_block(session, node)
    await session.refresh(execution)
    if race == "receipt":
        assert block["recorded_in"] == "execution"
        assert execution.status == ExecutionStatus.BLOCKED.value
    else:
        assert block["recorded_in"] == "decision"
        assert execution.revision == revision
        assert execution.claim_generation == identity.claim_generation
        assert execution.block_code is None
        assert block["authority_refusal"]
    assert block["block_code"] == (BlockCode.OWNERSHIP_LOST.value if race == "generation" else BlockCode.AUTHORITY_UNVERIFIABLE.value)


async def test_missing_execution_records_a_typed_hold_without_inventing_ownership(session, monkeypatch):
    _patch_results_installation(monkeypatch)
    node, dispatch = await _marked_story(session)
    await _bind(session, node, dispatch)
    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))
    assert report.errors == 0
    await session.commit()
    block = await _stored_handoff_block(session, node)
    assert block["block_code"] == BlockCode.AUTHORITY_UNVERIFIABLE.value
    assert block["recorded_in"] == "decision" and block["execution_id"] is None
    assert block["identity"] == {"org_id": node.org_id, "node_id": node.id, "cycle": node.attempts}
    assert list(await session.scalars(select(OrchestrationExecution))) == []


async def test_a_later_authorized_handoff_clears_the_block_and_completes(session, monkeypatch):
    _patch_results_installation(monkeypatch)
    node, dispatch = await _marked_story(session)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))
    assert (await _stored_handoff_block(session, node))["recorded_in"] == "execution"
    assert (await commit_handoff(session, identity=identity, now=datetime.now(UTC))).accepted
    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))
    assert report.errors == 0 and node.state == NodeState.PASSED.value
    row = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))
    assert row.block_code is None and row.handoff_receipt_ref
    assert "handoff_block" not in (await _observations(session, node))[-1]


@pytest.mark.parametrize("code", [BlockCode.HUMAN_GATE_REQUIRED, BlockCode.BUDGET_EXHAUSTED])
async def test_missing_handoff_preserves_existing_gate_and_pending_action(session, monkeypatch, code):
    _patch_results_installation(monkeypatch)
    node, dispatch = await _marked_story(session)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    record = (await load_execution(session, identity=identity)).record
    due = datetime.now(UTC) + timedelta(hours=1)
    existing = outstanding_block(code, owner="requester", required_input="resolve the existing gate", detail="original gate")
    outcome = await advance_execution(
        session,
        identity=identity,
        advance=PhaseAdvance(phase=record.phase, status=ExecutionStatus.BLOCKED, expected_revision=record.revision, next_check_at=due),
        block=existing,
        pending_action_key="pending-external-observation",
    )
    assert outcome.kind is OutcomeKind.BLOCKED
    before = outcome.record
    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))
    assert report.errors == 0
    await session.commit()
    after = (await load_execution(session, identity=identity)).record
    assert after == before
    block = await _stored_handoff_block(session, node)
    assert block["recorded_in"] == "decision"
    assert block["block_code"] == BlockCode.AUTHORITY_UNVERIFIABLE.value


async def test_handoff_block_and_node_hold_rollback_together_when_audit_write_fails(session, monkeypatch):
    _patch_results_installation(monkeypatch)
    node, dispatch = await _marked_story(session)
    await _bind(session, node, dispatch)
    identity = await _ledger(session, node)
    await session.commit()
    before = (await load_execution(session, identity=identity)).record
    state_before = node.state
    original_add = session.add

    def fail_result_audit(instance, *args, **kwargs):
        if isinstance(instance, OrchestrationDecision) and instance.kind == DecisionKind.RESULT_OBSERVED.value:
            raise RuntimeError("injected audit persistence failure")
        return original_add(instance, *args, **kwargs)

    monkeypatch.setattr(session, "add", fail_result_audit)
    report = await observe_results(session, run_store=_RunStore(node), evidence=StubSource(evidence=_green()))
    assert report.errors == 1
    await session.commit()
    await session.refresh(node)
    assert node.state == state_before
    assert (await load_execution(session, identity=identity)).record == before
    assert await _observations(session, node) == []
