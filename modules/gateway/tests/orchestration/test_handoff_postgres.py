"""Real-PostgreSQL concurrency tests for the handoff receipt (#5144).

`test_handoff.py` proves the *semantics* on SQLite and deliberately proves nothing
about locking, because `SELECT ... FOR UPDATE` is a **no-op** there. The guarantee this
story actually rests on is only observable with real concurrency:

> two workers reporting the same handoff at the same time produce **one** receipt.

That is not an exotic case, it is the expected one. A worker dies mid-delivery, SQS
redelivers, a tick restarts, and two processes report the same lane at once. If that
minted two receipts, each would look like a separate completed delivery and the engine
would believe the work was handed off twice — which is the double-effect the issue
exists to prevent, reintroduced by the fix.

The interleaving that matters is specifically the one SQLite's single-writer model
hides: both transactions read "no receipt" before either writes. Under the row lock the
second writer waits, observes the winner's committed state, and converges on the same
receipt; the unique index on `(org_id, node_id, cycle)` is the backstop underneath it.

`asyncio.gather` over separate sessions rather than threads: genuinely concurrent at the
database, while the failure mode stays reproducible instead of depending on OS thread
scheduling. Same reasoning as `test_execution_store_postgres.py`.

Skips (never silently passes) when no PostgreSQL server is available — `pgserver`
publishes wheels for Python <= 3.12 only, which CI's Test job uses. A skip here means
**"not tested"** and must be reported as such, not as a pass: `N skipped` reading as
clean is exactly how a concurrency guarantee goes unverified.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.execution_state import (
    TERMINAL_EXECUTION_STATUSES,
    ExecutionIdentity,
    ExecutionStatus,
    OutcomeKind,
)
from src.orchestration.execution_store import create_execution
from src.orchestration.handoff import HandoffOutcome, commit_handoff, receipt_for
from src.orchestration.models import (
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.work_claims import OwnerKind

# `pg_server` is session-scoped, so a run that also touches the migration or store
# tests shares one server rather than starting a second.
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

pytestmark = pytest.mark.integration

ORG_A = "org-alpha"
CLAIM = "claim-5144"
PLAN_VERSION = 3


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811 - pg_url is a fixture, not a shadowed import
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(OrchestrationFlow.__table__.create)
        await conn.run_sync(OrchestrationAcceptedPlan.__table__.create)
        await conn.run_sync(OrchestrationNode.__table__.create)
        await conn.run_sync(OrchestrationWorkClaim.__table__.create)
        await conn.run_sync(OrchestrationExecution.__table__.create)
        await conn.run_sync(OrchestrationAction.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def seeded(pg_session_factory):
    """A committed flow, plan, held claim, node and execution row.

    Committed rather than flushed, so every concurrent session genuinely sees them.
    """
    async with pg_session_factory() as session:
        flow = OrchestrationFlow(org_id=ORG_A, slug="flow-5144", title="Flow", state="draft")
        session.add(flow)
        await session.flush()
        session.add(OrchestrationAcceptedPlan(org_id=ORG_A, flow_id=flow.id, version=PLAN_VERSION, plan_document={}, plan_hash="plan-5144"))
        session.add(
            OrchestrationWorkClaim(
                id=CLAIM,
                org_id=ORG_A,
                provider_repository_id=5144,
                issue_number=5144,
                owner_kind=OwnerKind.ENGINE_FLOW.value,
                owner_ref=flow.id,
                state=ClaimState.HELD.value,
                generation=1,
            )
        )
        node = OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N1",
            kind="story",
            title="Node",
        )
        session.add(node)
        await session.flush()
        identity = ExecutionIdentity(
            org_id=ORG_A,
            node_id=node.id,
            cycle=1,
            accepted_plan_version=PLAN_VERSION,
            claim_id=CLAIM,
            claim_generation=1,
        )
        outcome = await create_execution(session, identity=identity, flow_id=flow.id)
        assert outcome.kind is OutcomeKind.APPLIED
        await session.commit()
    return identity


async def _report(pg_session_factory, identity: ExecutionIdentity, *, generation: int | None = None):
    """One independent worker reporting its handoff, in its own transaction."""
    used = (
        identity
        if generation is None
        else ExecutionIdentity(
            org_id=identity.org_id,
            node_id=identity.node_id,
            cycle=identity.cycle,
            accepted_plan_version=identity.accepted_plan_version,
            claim_id=identity.claim_id,
            claim_generation=generation,
        )
    )
    async with pg_session_factory() as session:
        result = await commit_handoff(session, identity=used, now=datetime.now(UTC))
        if result.accepted:
            await session.commit()
        else:
            await session.rollback()
        return result


async def _row(pg_session_factory, identity: ExecutionIdentity) -> OrchestrationExecution:
    async with pg_session_factory() as session:
        return (
            await session.execute(
                select(OrchestrationExecution).where(
                    OrchestrationExecution.org_id == identity.org_id,
                    OrchestrationExecution.node_id == identity.node_id,
                    OrchestrationExecution.cycle == identity.cycle,
                )
            )
        ).scalar_one()


async def test_simultaneous_reports_mint_exactly_one_receipt(pg_session_factory, seeded):
    """The guarantee SQLite cannot show: concurrent reports converge on one receipt.

    Both transactions read "no receipt" before either writes. Under the row lock the
    loser waits, observes the winner's committed state, and returns the identical
    receipt — rather than minting a second one or advancing the row twice.
    """
    results = await asyncio.gather(*(_report(pg_session_factory, seeded) for _ in range(4)))

    accepted = [r for r in results if r.accepted]
    assert accepted, f"no report succeeded: {[(r.outcome, r.reason) for r in results]}"
    # One receipt, not one per reporter.
    assert len({r.receipt_ref for r in accepted}) == 1
    assert sum(1 for r in results if r.outcome is HandoffOutcome.COMMITTED) == 1
    row = await _row(pg_session_factory, seeded)
    assert row.handoff_receipt_ref == accepted[0].receipt_ref


async def test_concurrent_reports_advance_the_row_only_once(pg_session_factory, seeded):
    """No counter moves per reporter.

    A revision that advanced once per report would read as several successive
    deliveries, which is the defect's signature rather than its fix.
    """
    before = (await _row(pg_session_factory, seeded)).revision

    await asyncio.gather(*(_report(pg_session_factory, seeded) for _ in range(5)))

    assert (await _row(pg_session_factory, seeded)).revision == before + 1


async def test_the_committed_continuation_is_never_terminal_under_concurrency(pg_session_factory, seeded):
    """Whichever writer wins, the lane stays due. No race can produce completion."""
    await asyncio.gather(*(_report(pg_session_factory, seeded) for _ in range(4)))

    row = await _row(pg_session_factory, seeded)
    assert ExecutionStatus(row.status) not in TERMINAL_EXECUTION_STATUSES
    assert row.next_check_at is not None


async def test_a_displaced_worker_racing_the_new_owner_is_refused(pg_session_factory, seeded):
    """A superseded generation cannot win the race, in either arrival order.

    This is the ownership-transfer case: the old attempt must not have the new owner's
    receipt handed to it, and must not overwrite it.
    """
    async with pg_session_factory() as session:
        claim = await session.get(OrchestrationWorkClaim, CLAIM)
        claim.generation = 2
        await session.commit()

    stale, live = await asyncio.gather(
        _report(pg_session_factory, seeded, generation=1),
        _report(pg_session_factory, seeded, generation=2),
    )

    assert stale.accepted is False
    # Exactly one receipt exists, and it is not the stale attempt's.
    row = await _row(pg_session_factory, seeded)
    if live.accepted:
        assert row.handoff_receipt_ref == live.receipt_ref
    assert row.handoff_receipt_ref != stale.receipt_ref


async def test_a_refused_racer_leaves_the_work_due_rather_than_half_written(pg_session_factory, seeded):
    """A refusal writes nothing. The durable outcome is "still due", not a partial row."""
    async with pg_session_factory() as session:
        claim = await session.get(OrchestrationWorkClaim, CLAIM)
        claim.generation = 5
        await session.commit()

    results = await asyncio.gather(*(_report(pg_session_factory, seeded, generation=1) for _ in range(3)))

    assert not any(r.accepted for r in results)
    row = await _row(pg_session_factory, seeded)
    assert row.handoff_receipt_ref is None
    assert row.next_check_at is not None
    assert ExecutionStatus(row.status) not in TERMINAL_EXECUTION_STATUSES


async def test_a_repeat_long_after_the_first_still_returns_the_same_receipt(pg_session_factory, seeded):
    """Convergence is not a within-transaction artifact; it survives commit boundaries."""
    first = await _report(pg_session_factory, seeded)
    assert first.accepted is True

    repeat = await _report(pg_session_factory, seeded)

    assert repeat.outcome is HandoffOutcome.ALREADY_COMMITTED
    assert repeat.receipt_ref == first.receipt_ref


# ---------------------------------------------------------------------------
# F2: the receipt's authority is revalidated at commit time, under real locks
# ---------------------------------------------------------------------------
#
# `test_handoff_reconciliation.py` proves the revalidation is reached and agrees with
# itself, and deliberately proves nothing about the race — SQLite serializes writers
# and `FOR UPDATE` is a no-op there. The blocker is specifically about a window:
#
#   `_story_evidence` reads the receipt with NO locks (alongside the provider call)
#   → a handover commits and advances the claim generation
#   → `observe_results` takes the node lock and writes PASSED
#
# Written on the pre-handover snapshot, that PASSED rests on a receipt `receipt_for`
# can no longer attribute to any attempt: the node reads as complete while its
# continuation is unaccounted for. That is the #5144 defect reached through a race
# instead of through a clean exit, which is why a lock-order-correct re-read has to
# happen inside the same transaction as the write.


async def _handover(pg_session_factory, identity: ExecutionIdentity) -> None:
    """Commit an ownership handover, as a concurrent recovery or `/handoff` would.

    Its own committed transaction, so the sweep genuinely observes it rather than
    seeing an uncommitted sibling's state.
    """
    async with pg_session_factory() as session:
        claim = await session.get(OrchestrationWorkClaim, identity.claim_id)
        claim.generation += 1
        await session.commit()


async def _receipt_under_lock(pg_session_factory, identity: ExecutionIdentity) -> str | None:
    """The commit-time revalidation, in its own transaction and holding its locks."""
    async with pg_session_factory() as session:
        answer = await receipt_for(session, identity=identity, lock=True)
        await session.commit()
        return answer


async def test_a_handover_committed_before_the_locked_reread_is_observed(pg_session_factory, seeded):
    """The blocker's exact failure: the lock-free snapshot is stale and the re-read says so.

    The unlocked read is taken first — standing in for `_story_evidence`, which runs it
    alongside the provider call. The handover then commits in the window. The locked
    re-read is what `observe_results` now performs before writing PASSED, and it must
    return `None`: the receipt still sits on the row, but it can no longer be
    attributed to this attempt.

    Asserted as "the two reads disagree", which is the only thing that makes the
    revalidation worth doing. If they always agreed it would be dead code.
    """
    async with pg_session_factory() as session:
        snapshot = await receipt_for(session, identity=seeded)
    assert snapshot is None, "no receipt has been committed yet"

    assert (await _report(pg_session_factory, seeded)).accepted is True
    async with pg_session_factory() as session:
        before = await receipt_for(session, identity=seeded)
    assert before is not None, "the receipt is attributable before the handover"

    await _handover(pg_session_factory, seeded)

    assert await _receipt_under_lock(pg_session_factory, seeded) is None
    # The row was not repaired or cleared — the receipt is still there, and that is the
    # point. Presence was never the question; attribution is.
    assert (await _row(pg_session_factory, seeded)).handoff_receipt_ref == before


async def test_the_lock_makes_a_concurrent_handover_wait_for_the_verdict(pg_session_factory, seeded):
    """`lock=True` is load-bearing: a handover cannot commit while the verdict is open.

    This is the claim `lock=True` actually buys, and the one the other tests here do
    NOT make. They use short separate transactions, so dropping the lock leaves them
    green — a re-read that merely happens *later* is still only a snapshot. What makes
    the revalidation sound is that it holds its answer until the transaction that acts
    on it commits, so nothing can invalidate the verdict in between.

    Driven by ordering rather than by sleeping on a clock: the reader takes the lock,
    signals, and waits to be told to commit; the handover runs in that window and
    records when it finished. If the claim row were not locked, the handover would
    commit *before* the reader — which is precisely the window F2 is about, and is what
    makes this test fail with `lock=False`.

    `asyncio.wait_for` on the reader's gate so a regression that never acquires the
    lock fails as an assertion rather than hanging the suite.
    """
    assert (await _report(pg_session_factory, seeded)).accepted is True
    locked = asyncio.Event()
    order: list[str] = []

    async def reader() -> str | None:
        async with pg_session_factory() as session:
            answer = await receipt_for(session, identity=seeded, lock=True)
            locked.set()
            # Long enough that a handover free to proceed certainly would have.
            await asyncio.sleep(0.5)
            order.append("verdict-committed")
            await session.commit()
            return answer

    async def handover() -> None:
        await asyncio.wait_for(locked.wait(), timeout=10)
        await _handover(pg_session_factory, seeded)
        order.append("handover-committed")

    answer, _ = await asyncio.gather(reader(), handover())

    # The verdict was reached on the pre-handover authority AND could not be
    # invalidated before the reader was done with it.
    assert answer is not None
    assert order == ["verdict-committed", "handover-committed"]
    # The handover did land — so the test proves serialization, not that it was blocked
    # forever.
    assert await _receipt_under_lock(pg_session_factory, seeded) is None


async def test_the_locked_reread_still_confirms_an_undisturbed_receipt(pg_session_factory, seeded):
    """The positive case under real locks, so the guard is a gate and not a wall.

    Without this, a revalidation that deadlocked, timed out, or simply always returned
    `None` would satisfy every test above while holding every story in production.
    """
    result = await _report(pg_session_factory, seeded)
    assert result.accepted is True

    assert await _receipt_under_lock(pg_session_factory, seeded) == result.receipt_ref
    # Repeatable: the locked read is not a one-shot that consumes what it verified.
    assert await _receipt_under_lock(pg_session_factory, seeded) == result.receipt_ref
