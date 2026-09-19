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

from datetime import UTC, datetime

from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
from src.orchestration.execution_store import create_execution
from src.orchestration.handoff import commit_handoff, handoff_required, missing_receipt_hold
from src.orchestration.models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationWorkClaim,
)
from src.orchestration.results import _story_evidence
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
