"""Real-PostgreSQL concurrency tests for the environment lease store (#5150).

This file is where the story's central claim is actually tested, and SQLite cannot
substitute for it:

- `SELECT ... FOR UPDATE` is a **no-op** on SQLite. The row lock that makes the
  second acquirer *wait* and then observe the winner's committed state does not
  exist there, so `test_environment_leases.py` proves the *semantics* and
  deliberately proves nothing about locking.
- The global unique index on `canonical_target_key` is the real correctness
  backstop, and `IntegrityError` from a concurrent insert is the path
  `acquire_lease` converts into a re-read of the committed holder. That path is
  unreachable without two genuinely concurrent writers.
- SQLite's single-writer model hides the exact interleaving that produces the
  double-hold: two transactions that both read "this target is free" before either
  inserts.

Why that matters concretely rather than in the abstract: the interleaving is the
*expected* case, not an exotic one. Two tenants' connections can name one cluster,
two nodes of one plan can target one namespace, and a redelivered message can
restart a deploy while the first is still running. If two acquisitions both
succeeded, two pipelines would deploy incompatible releases onto one cluster — the
precise failure this table exists to prevent, and one no amount of application-level
"is it free?" checking can close.

`asyncio.gather` over separate sessions, not threads: the sessions are genuinely
concurrent at the database while the failure mode stays reproducible instead of
depending on OS thread scheduling. Same reasoning `test_work_claims_postgres.py` and
`test_execution_store_postgres.py` give.

Skips (never silently passes) when no PostgreSQL server is available — `pgserver`
publishes wheels for Python <= 3.12 only, which CI's Test job uses. A skip here
means **"not tested"**, and this PR reports it as such rather than as a pass.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.deployment_manifest import PhysicalTarget, TargetEvidence
from src.orchestration.environment_leases import (
    LeaseError,
    LeaseHolder,
    LeaseOutcomeKind,
    LeaseState,
    ReleaseReason,
    acquire_lease,
    begin_shared_publication,
    reconcile_lease,
    record_shared_publication_absence,
    recover_shared_publication,
    release_lease,
)
from src.orchestration.models import OrchestrationEnvironmentLease
from src.orchestration.operations_publication_recovery import ImageAbsence, ImageReadback

# `pg_server` is session-scoped, so a run that also touches the migration tests
# shares one server. Imported explicitly because this file lives in
# tests/orchestration/ rather than tests/migrations/.
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

pytestmark = pytest.mark.integration

ORG_A = "org-alpha"
ORG_B = "org-beta"
ENTRY = "adp-dev-embark1-gateway"
EVIDENCE_TEXT = "workflow run 12345 concluded: success"


def _target(*, resource_id: str = "cluster-a/adp-gateway", connection: str = "conn-1") -> PhysicalTarget:
    """One physical surface, reachable through a named connection.

    `connection` only varies the *evidence*, never the identity — which is the whole
    point: two aliases differ in how they were proven and agree on what they name.
    `000000000000` is a placeholder rather than a plausible account, because the
    story forbids inventing account ids and a realistic-looking one could be copied
    into configuration by somebody skimming.
    """
    return PhysicalTarget(
        provider="aws",
        account_id="000000000000",
        region="us-east-1",
        resource_kind="eks-namespace",
        resource_id=resource_id,
        evidence=TargetEvidence(
            source=f"verified-aws-connection:{connection}",
            verified_at="2026-09-18T00:00:00+00:00",
            detail="sts:AssumeRole readback",
        ),
    )


def _holder(org: str = ORG_A, action: str = "action-1", generation: int = 1) -> LeaseHolder:
    return LeaseHolder(org_id=org, action_id=action, generation=generation)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811 - pg_url is a fixture, not a shadowed import
    """An async engine on a fresh PostgreSQL database with only the lease table.

    Built straight from the ORM model rather than by running the Alembic chain: this
    file tests runtime concurrency, so creating only the table under test keeps it
    independent of unrelated migrations. The lease table has no foreign keys, so
    nothing else is needed. Migration correctness — including that the DDL matches
    this model and that the unique index is NOT tenant-scoped — is
    `tests/migrations/test_055_orchestration_environment_leases.py` instead.
    """
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(OrchestrationEnvironmentLease.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


async def _acquire_committed(factory, target, holder, entry=ENTRY, **kwargs):
    """Acquire in its own session and commit, so other sessions can see it.

    Each concurrent participant gets its own session because the store commits
    nothing itself — the caller owns the transaction boundary, and that is exactly
    the boundary this file is exercising.
    """
    async with factory() as session:
        outcome = await acquire_lease(session, target=target, holder=holder, manifest_entry_id=entry, **kwargs)
        await session.commit()
        return outcome


async def _race_acquire(factory, participants):
    """Acquire from several sessions that have all read the target as free first.

    This is the difference between a test that exercises the unique index and one
    that only looks like it does. Bare `asyncio.gather` over acquire-and-commit
    coroutines can serialize — the first commits before the second begins — and then
    the second is refused by the store's application-level "is it held?" check, which
    a *tenant-scoped* index would satisfy just as well. The double-hold only becomes
    reachable when both transactions have observed an empty target before either
    inserts, so that state is arranged explicitly here.

    Each participant: opens its own session, performs a read of the target, waits at
    the barrier until every participant has done the same, and only then attempts the
    acquire and commits.
    """
    barrier = asyncio.Barrier(len(participants))

    async def participant(target, holder, entry):
        async with factory() as session:
            # The pre-read: establishes "I saw this target as free" inside this
            # transaction, exactly as a real caller's admission check would.
            await session.execute(
                select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
            )
            await barrier.wait()
            try:
                outcome = await acquire_lease(session, target=target, holder=holder, manifest_entry_id=entry)
                await session.commit()
                return outcome
            except Exception:
                await session.rollback()
                raise

    return await asyncio.gather(*(participant(*p) for p in participants), return_exceptions=True)


async def _row_count(factory, key: str) -> int:
    async with factory() as session:
        return (
            await session.execute(
                select(func.count()).select_from(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == key)
            )
        ).scalar_one()


async def _fetch(factory, key: str) -> OrchestrationEnvironmentLease:
    async with factory() as session:
        return (
            await session.execute(select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == key))
        ).scalar_one()


class TestConcurrentAcquisition:
    """THE test of this issue: exactly one winner per physical target."""

    async def test_two_aliases_in_two_tenants_admit_exactly_one_holder(self, pg_session_factory):
        """Two tenants, two connection ids, one real cluster, both racing.

        This is the scenario the story is built around. If both succeeded, two
        pipelines would deploy incompatible releases onto one cluster. Note that
        nothing in the application layer can prevent it: both transactions legitimately
        read "this target is free" before either writes, so only the global unique
        index can refuse the second.
        """
        alias_a = _target(connection="conn-tenant-a")
        alias_b = _target(connection="conn-tenant-b")
        assert alias_a.canonical_key == alias_b.canonical_key

        # A barrier, not bare `gather`. Without it the two coroutines can serialize —
        # the first commits before the second ever reads — and the test then passes
        # against a *tenant-scoped* unique index, because the second caller sees the
        # committed holder through the application-level check rather than being
        # refused by the constraint. The barrier forces the interleaving that only the
        # global index can close: both transactions read "this target is free", and
        # only then does either write.
        outcomes = await _race_acquire(
            pg_session_factory,
            [
                (alias_a, _holder(ORG_A, "action-a"), ENTRY),
                (alias_b, _holder(ORG_B, "action-b"), "other-entry"),
            ],
        )

        applied = [o for o in outcomes if getattr(o, "kind", None) is LeaseOutcomeKind.APPLIED]
        refused = [o for o in outcomes if getattr(o, "kind", None) is LeaseOutcomeKind.CONFLICT]
        raced = [o for o in outcomes if isinstance(o, LeaseError)]

        # Exactly one holder. The loser either sees the committed winner (CONFLICT)
        # or, if the winner had not committed when it looked, gets the typed
        # `acquire_race_lost` refusal — which is also a refusal to proceed. What must
        # never happen is two APPLIED.
        assert len(applied) == 1, outcomes
        assert len(refused) + len(raced) == 1, outcomes
        for error in raced:
            assert error.code == "acquire_race_lost"

        assert await _row_count(pg_session_factory, alias_a.canonical_key) == 1

    async def test_many_concurrent_acquirers_produce_one_holder_and_one_row(self, pg_session_factory):
        """Eight racers, one target. Fan-out widens the interleaving window.

        Two racers can miss an interleaving that eight reliably hit; this is what
        exercises both the FOR UPDATE wait path and the IntegrityError path in one run.
        """
        target = _target()
        outcomes = await _race_acquire(
            pg_session_factory,
            [(_target(connection=f"conn-{index}"), _holder(ORG_A, f"action-{index}"), ENTRY) for index in range(8)],
        )

        applied = [o for o in outcomes if getattr(o, "kind", None) is LeaseOutcomeKind.APPLIED]
        assert len(applied) == 1, outcomes
        # No participant may be told anything but "held"/race-lost.
        for outcome in outcomes:
            if outcome in applied:
                continue
            if isinstance(outcome, LeaseError):
                assert outcome.code == "acquire_race_lost"
            else:
                assert outcome.kind is LeaseOutcomeKind.CONFLICT
                assert outcome.reason == "target_held"
                assert outcome.lease is None
        assert await _row_count(pg_session_factory, target.canonical_key) == 1

    async def test_unrelated_targets_proceed_concurrently(self, pg_session_factory):
        """Independent surfaces must not block each other.

        If the identity were account-level — or if the lock were taken on something
        coarser than the surface — these four independent namespaces would serialize.
        A lease that blocks unrelated work trains operators to bypass it, and a lease
        people bypass protects nothing.
        """
        targets = [_target(resource_id=f"cluster-a/ns-{index}") for index in range(4)]
        outcomes = await asyncio.gather(
            *(_acquire_committed(pg_session_factory, t, _holder(ORG_A, f"action-{index}")) for index, t in enumerate(targets))
        )
        assert all(o.applied for o in outcomes)
        assert len({o.lease.canonical_target_key for o in outcomes}) == 4

    async def test_contended_acquirer_waits_and_sees_the_committed_winner(self, pg_session_factory):
        """`FOR UPDATE` without `skip_locked`: the second caller waits.

        Skipping would read "no lease here" and insert a second row — for a lease,
        silent skipping IS the double-deploy. The assertion is behavioural: while a
        holder's transaction is open, a second acquirer does not complete; once the
        holder commits, the second observes the committed state and refuses.
        """
        target = _target()
        holder_committed = asyncio.Event()

        async def winner():
            async with pg_session_factory() as session:
                outcome = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
                # Hold the transaction open briefly so the contender is genuinely
                # contending rather than arriving after the fact.
                await asyncio.sleep(0.3)
                await session.commit()
                holder_committed.set()
                return outcome

        async def contender():
            # Start after the winner has begun but before it commits.
            await asyncio.sleep(0.1)
            assert not holder_committed.is_set()
            return await _acquire_committed(pg_session_factory, _target(connection="conn-2"), _holder(ORG_B, "action-b"))

        first, second = await asyncio.gather(winner(), contender(), return_exceptions=True)
        assert first.applied
        if isinstance(second, LeaseError):
            assert second.code == "acquire_race_lost"
        else:
            assert second.kind is LeaseOutcomeKind.CONFLICT
        assert await _row_count(pg_session_factory, target.canonical_key) == 1

    async def test_refusal_under_real_contention_leaks_no_holder_detail(self, pg_session_factory):
        """A cross-tenant probe oracle would be worse here than a failed deploy.

        A refusal that named the holder would let any tenant discover the existence,
        ownership and deployment activity of another tenant's infrastructure simply by
        asking for a busy target.
        """
        await _acquire_committed(pg_session_factory, _target(connection="conn-a"), _holder(ORG_A, "action-a"))
        refused = await _acquire_committed(pg_session_factory, _target(connection="conn-b"), _holder(ORG_B, "action-b"), entry="entry-b")
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.lease is None
        rendered = str(refused.reason)
        for leaked in (ORG_A, "action-a", ENTRY, "000000000000", "cluster-a", "conn-a"):
            assert leaked not in rendered


class TestConcurrentTransitions:
    """Compare-and-set under real concurrency: one winner, one retryable loser."""

    async def test_concurrent_reconcile_yields_one_applied_and_one_stale(self, pg_session_factory):
        """Both callers read the same revision; only one may write.

        The loser must be STALE — retryable — because whatever moved the row may have
        changed what it should record. Being told CONFLICT would make it stop when it
        should re-read, and being told APPLIED would mean a lost update.
        """
        target = _target()
        first = await _acquire_committed(pg_session_factory, target, _holder())
        revision = first.lease.revision

        async def reconcile(note: str):
            async with pg_session_factory() as session:
                outcome = await reconcile_lease(
                    session,
                    canonical_target_key=target.canonical_key,
                    expected_revision=revision,
                    terminal_evidence=note,
                    # Both racers ARE the holder — this tests the compare-and-set
                    # between two processes of one action, not authority.
                    holder=_holder(),
                )
                await session.commit()
                return outcome

        outcomes = await asyncio.gather(reconcile("reading-one"), reconcile("reading-two"))
        kinds = sorted(o.kind.value for o in outcomes)
        assert kinds == ["applied", "stale"]
        row = await _fetch(pg_session_factory, target.canonical_key)
        assert row.revision == revision + 1
        assert row.reconciled_terminal_evidence in {"reading-one", "reading-two"}

    async def test_concurrent_release_and_acquire_do_not_overlap(self, pg_session_factory):
        """A release racing a stranger's acquire must not produce two holders.

        The dangerous outcome is the acquire slipping in *between* the release's read
        and its write, landing on a row the release then overwrites as free — leaving
        the new holder's deployment running against a target the table says is
        available.
        """
        target = _target()
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a"))

        async def releasing():
            async with pg_session_factory() as session:
                outcome = await release_lease(
                    session,
                    canonical_target_key=target.canonical_key,
                    holder=_holder(ORG_A, "action-a"),
                    reason=ReleaseReason.COMPLETED,
                    terminal_evidence=EVIDENCE_TEXT,
                )
                await session.commit()
                return outcome

        async def acquiring():
            return await _acquire_committed(pg_session_factory, _target(connection="conn-b"), _holder(ORG_B, "action-b"), entry="entry-b")

        released, acquired = await asyncio.gather(releasing(), acquiring())
        assert released.applied
        row = await _fetch(pg_session_factory, target.canonical_key)
        if acquired.kind is LeaseOutcomeKind.APPLIED:
            # The acquire won the ordering: the target must be HELD by it, never left
            # free by the release that ran alongside.
            assert row.state == LeaseState.HELD.value
            assert row.owner_action_id == "action-b"
        else:
            assert acquired.kind is LeaseOutcomeKind.CONFLICT
            assert row.state == LeaseState.FREE.value


class TestExpiryDoesNotLicenseTakeover:
    """The story's other central rule, asserted where locking is real."""

    async def test_lapsed_unreconciled_lease_blocks_every_concurrent_takeover(self, pg_session_factory):
        """A partitioned pipeline is still rolling pods.

        Four racers all find a lapsed lease. If expiry alone licensed takeover, one
        (or worse, several) would proceed while the original deployment is potentially
        still running. All four must be refused.
        """
        target = _target()
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a"))
        async with pg_session_factory() as session:
            await session.execute(
                update(OrchestrationEnvironmentLease)
                .where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(hours=2))
            )
            await session.commit()

        outcomes = await asyncio.gather(
            *(
                _acquire_committed(pg_session_factory, _target(connection=f"conn-{i}"), _holder(ORG_B, f"action-{i}"), entry="entry-b")
                for i in range(4)
            ),
            return_exceptions=True,
        )
        for outcome in outcomes:
            assert getattr(outcome, "kind", None) is LeaseOutcomeKind.CONFLICT, outcome
            assert outcome.reason == "target_held"

        row = await _fetch(pg_session_factory, target.canonical_key)
        assert row.owner_action_id == "action-a"

    async def test_reconciled_lapsed_lease_admits_exactly_one_successor(self, pg_session_factory):
        """Once somebody has looked, takeover is permitted — but only once.

        The pair of assertions matters: the gate opens, and it still admits a single
        winner. A gate that opened for everybody would replace one double-deploy with
        another.
        """
        target = _target()
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a"))
        async with pg_session_factory() as session:
            await session.execute(
                update(OrchestrationEnvironmentLease)
                .where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
                .values(
                    lease_expires_at=datetime.now(UTC) - timedelta(hours=2),
                    reconciled_terminal_evidence=EVIDENCE_TEXT,
                    reconciled_at=datetime.now(UTC),
                )
            )
            await session.commit()

        outcomes = await asyncio.gather(
            *(
                _acquire_committed(
                    pg_session_factory,
                    _target(connection=f"conn-{i}"),
                    _holder(ORG_B, f"action-{i}", generation=2),
                    entry="entry-b",
                )
                for i in range(4)
            ),
            return_exceptions=True,
        )
        applied = [o for o in outcomes if getattr(o, "kind", None) is LeaseOutcomeKind.APPLIED]
        assert len(applied) == 1, outcomes
        row = await _fetch(pg_session_factory, target.canonical_key)
        assert row.owner_org_id == ORG_B
        # The previous action's evidence must not carry forward and pre-clear the
        # NEXT lapse for the new holder.
        assert row.reconciled_terminal_evidence is None


class TestStaleGenerationUnderConcurrency:
    """A displaced actor cannot act, even when it races its successor."""

    async def test_stale_generation_cannot_release_the_winning_lease(self, pg_session_factory):
        """THE guard, asserted against real row locking.

        If the stale release were applied, it would free the target *while the current
        owner is deploying to it*, handing the cluster to whoever asks next. The stale
        actor must be refused terminally, and the row must be left held.
        """
        target = _target()
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a", generation=1))
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a", generation=5))

        async def stale_release():
            async with pg_session_factory() as session:
                outcome = await release_lease(
                    session,
                    canonical_target_key=target.canonical_key,
                    holder=_holder(ORG_A, "action-a", generation=1),
                    reason=ReleaseReason.COMPLETED,
                    terminal_evidence=EVIDENCE_TEXT,
                )
                await session.commit()
                return outcome

        async def current_heartbeat():
            async with pg_session_factory() as session:
                current = await _fetch(pg_session_factory, target.canonical_key)
                outcome = await reconcile_lease(
                    session,
                    canonical_target_key=target.canonical_key,
                    expected_revision=current.revision,
                    terminal_evidence="",
                    heartbeat=True,
                    holder=_holder(ORG_A, "action-a", generation=5),
                )
                await session.commit()
                return outcome

        stale, beat = await asyncio.gather(stale_release(), current_heartbeat(), return_exceptions=True)

        assert stale.kind is LeaseOutcomeKind.CONFLICT
        assert stale.reason == "owner_generation_superseded"
        # The heartbeat either applied or lost the CAS; what it must never be is a
        # conflict, because it holds current authority.
        if not isinstance(beat, LeaseError):
            assert beat.kind in {LeaseOutcomeKind.APPLIED, LeaseOutcomeKind.STALE}

        row = await _fetch(pg_session_factory, target.canonical_key)
        assert row.state == LeaseState.HELD.value
        assert row.owner_generation == 5

    async def test_concurrent_stale_releases_all_refuse(self, pg_session_factory):
        """Retrying a terminal refusal must not eventually succeed.

        Terminal means terminal — a displaced actor hammering release must never find
        an interleaving that lets it through.
        """
        target = _target()
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a", generation=1))
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a", generation=9))

        async def stale_release():
            async with pg_session_factory() as session:
                outcome = await release_lease(
                    session,
                    canonical_target_key=target.canonical_key,
                    holder=_holder(ORG_A, "action-a", generation=1),
                    reason=ReleaseReason.ABANDONED,
                    terminal_evidence=EVIDENCE_TEXT,
                )
                await session.commit()
                return outcome

        outcomes = await asyncio.gather(*(stale_release() for _ in range(5)))
        assert all(o.kind is LeaseOutcomeKind.CONFLICT for o in outcomes)
        row = await _fetch(pg_session_factory, target.canonical_key)
        assert row.state == LeaseState.HELD.value


class TestReconcileAuthorityUnderContention:
    """Terminal evidence is writable only by the holder — proven with real row locks.

    The SQLite suite asserts the same refusals, but on SQLite `SELECT ... FOR UPDATE`
    is a no-op, so it cannot show that the check holds when a foreign caller's
    reconcile genuinely overlaps the holder's own writes. That overlap is where an
    ordering bug would actually surface: the attacker's read and the holder's write
    interleave, and a check that consulted a stale in-session copy of the row would
    pass its unit test and fail here.
    """

    async def test_concurrent_foreign_reconciles_all_refuse_and_write_nothing(self, pg_session_factory):
        """Six tenants, each with its own alias, all trying to manufacture takeover.

        Every one must be refused and the row must be left with no reconciled
        evidence, because that evidence is the only thing that unblocks takeover of a
        lapsed target.
        """
        target = _target()
        first = await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a"))
        revision = first.lease.revision

        barrier = asyncio.Barrier(6)

        async def foreign(index: int):
            async with pg_session_factory() as session:
                await barrier.wait()
                outcome = await reconcile_lease(
                    session,
                    canonical_target_key=target.canonical_key,
                    expected_revision=revision,
                    terminal_evidence=f"fabricated-{index}",
                    holder=_holder(f"org-intruder-{index}", f"action-{index}"),
                )
                await session.commit()
                return outcome

        outcomes = await asyncio.gather(*(foreign(i) for i in range(6)))

        for outcome in outcomes:
            assert outcome.kind is LeaseOutcomeKind.CONFLICT
            assert outcome.reason == "target_held"
            assert outcome.lease is None

        row = await _fetch(pg_session_factory, target.canonical_key)
        assert row.reconciled_terminal_evidence is None
        assert row.revision == revision, "a refused reconcile must not advance the revision"

    async def test_foreign_reconcile_cannot_unblock_a_lapsed_takeover(self, pg_session_factory):
        """The end-to-end attack, on real PostgreSQL.

        Org B holds a legitimate alias for the cluster org A is deploying to. It
        fabricates evidence, waits for the contact window to lapse, and tries to take
        the target. Both steps must fail, and the second is the one that matters.
        """
        target_a = _target(connection="conn-A")
        await _acquire_committed(pg_session_factory, target_a, _holder(ORG_A, "action-a"))

        target_b = _target(connection="conn-B")
        assert target_b.canonical_key == target_a.canonical_key

        async with pg_session_factory() as session:
            refused = await reconcile_lease(
                session,
                canonical_target_key=target_b.canonical_key,
                expected_revision=1,
                terminal_evidence="fabricated: run 999 concluded success",
                holder=_holder(ORG_B, "action-b"),
            )
            await session.commit()
        assert refused.kind is LeaseOutcomeKind.CONFLICT

        # Lapse the contact window without any reconciled evidence.
        async with pg_session_factory() as session:
            await session.execute(
                update(OrchestrationEnvironmentLease)
                .where(OrchestrationEnvironmentLease.canonical_target_key == target_a.canonical_key)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()

        stolen = await _acquire_committed(pg_session_factory, target_b, _holder(ORG_B, "action-b"))
        assert stolen.kind is LeaseOutcomeKind.CONFLICT
        assert stolen.lease is None

    async def test_foreign_caller_cannot_distinguish_stale_from_held(self, pg_session_factory):
        """Indistinguishability while the holder is concurrently moving the revision.

        A foreign caller racing the holder's own writes gets the same opaque answer
        whatever revision it guesses; otherwise the difference is an oracle for when
        another tenant is deploying.
        """
        target = _target()
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a"))

        async def foreign(revision: int):
            async with pg_session_factory() as session:
                return await reconcile_lease(
                    session,
                    canonical_target_key=target.canonical_key,
                    expected_revision=revision,
                    terminal_evidence="probe",
                    holder=_holder(ORG_B, "action-b"),
                )

        answers = await asyncio.gather(foreign(1), foreign(2), foreign(9999))
        shapes = {(a.kind, a.reason, a.lease) for a in answers}
        assert len(shapes) == 1, f"a foreign caller learned something from the revision: {shapes}"
        assert answers[0].lease is None


class TestEvidenceRefreshUnderContention:
    """A new holder's row records the readback that authorized ITS hold.

    One durable row per physical target outlives every hold, so without an explicit
    refresh the row keeps whichever tenant's evidence inserted it — and
    `evidence_source` is what an operator reads to answer "what proved these two
    aliases name the same cluster?".
    """

    async def test_winner_of_a_cross_tenant_race_owns_the_evidence(self, pg_session_factory):
        """Whoever wins, the stored evidence must be the winner's, not the insert's.

        Under a genuine race the winner is not known in advance, which is what makes
        this stronger than the sequential case: the assertion has to hold for either
        outcome, so it cannot accidentally be satisfied by insert-time values.
        """
        participants = [
            (_target(connection="conn-A"), _holder(ORG_A, "action-a"), ENTRY),
            (_target(connection="conn-B"), _holder(ORG_B, "action-b"), ENTRY),
        ]
        outcomes = await _race_acquire(pg_session_factory, participants)

        applied = [o for o in outcomes if getattr(o, "kind", None) is LeaseOutcomeKind.APPLIED]
        assert len(applied) == 1
        winner = applied[0]

        row = await _fetch(pg_session_factory, participants[0][0].canonical_key)
        assert row.evidence_source == winner.lease.evidence_source
        assert row.evidence_source in {"verified-aws-connection:conn-A", "verified-aws-connection:conn-B"}

    async def test_reacquire_after_release_replaces_the_previous_tenants_evidence(self, pg_session_factory):
        target_a = _target(connection="conn-A")
        await _acquire_committed(pg_session_factory, target_a, _holder(ORG_A, "action-a"))
        async with pg_session_factory() as session:
            await release_lease(
                session,
                canonical_target_key=target_a.canonical_key,
                holder=_holder(ORG_A, "action-a"),
                reason=ReleaseReason.COMPLETED,
                terminal_evidence="run 1 concluded: success",
            )
            await session.commit()

        target_b = _target(connection="conn-B")
        taken = await _acquire_committed(pg_session_factory, target_b, _holder(ORG_B, "action-b"))
        assert taken.applied

        row = await _fetch(pg_session_factory, target_a.canonical_key)
        assert row.evidence_source == "verified-aws-connection:conn-B"
        # One row still, across both tenants' aliases.
        assert await _row_count(pg_session_factory, target_a.canonical_key) == 1


class TestUniqueIndexIsGlobal:
    """The index that makes all of the above possible."""

    async def test_second_row_for_one_target_is_refused_by_the_database(self, pg_session_factory):
        """Asserted at the DDL level, not just through the store.

        The store's application-level checks are defence in depth; this is the
        constraint they rest on. If it were scoped by org_id, two tenants' aliases for
        one cluster could both be held — and every store-level test would still pass,
        because the store never asks the database to enforce tenancy here.
        """
        target = _target()
        await _acquire_committed(pg_session_factory, target, _holder(ORG_A, "action-a"))

        async with pg_session_factory() as session:
            session.add(
                OrchestrationEnvironmentLease(
                    canonical_target_key=target.canonical_key,
                    evidence_source="verified-aws-connection:conn-b",
                    evidence_verified_at=datetime.now(UTC),
                    state=LeaseState.HELD.value,
                    # A DIFFERENT tenant. This is the insert an org-scoped index would
                    # happily accept.
                    owner_org_id=ORG_B,
                    owner_action_id="action-b",
                    owner_generation=1,
                    manifest_entry_id="entry-b",
                    revision=1,
                    created_at=datetime.now(UTC),
                )
            )
            with pytest.raises(Exception) as exc:
                await session.commit()
            assert "unique" in str(exc.value).lower() or "duplicate" in str(exc.value).lower()

        assert await _row_count(pg_session_factory, target.canonical_key) == 1


async def test_shared_release_scope_fences_different_surfaces_and_isolates_phase_fixtures(pg_session_factory, record_property):
    from src.orchestration.environment_leases import isolated_release_fixture, shared_release_target

    gateway = shared_release_target(_target(resource_id="cluster-a/gateway"), environment="dev", group="tool-connectors")
    facade = shared_release_target(_target(resource_id="cluster-a/facade"), environment="dev", group="tool-connectors")
    other_environment = shared_release_target(_target(resource_id="cluster-a/facade"), environment="stage", group="tool-connectors")
    assert gateway.canonical_key == facade.canonical_key
    assert gateway.canonical_key != other_environment.canonical_key
    assert isolated_release_fixture(gateway, owner="phase-one") != isolated_release_fixture(facade, owner="phase-two")
    owner = _holder(ORG_A, "phase-one")
    successor = _holder(ORG_B, "phase-two")
    first = await _acquire_committed(pg_session_factory, gateway, owner)
    assert first.applied
    competing = await _acquire_committed(pg_session_factory, facade, successor)
    assert competing.kind is LeaseOutcomeKind.CONFLICT
    async with pg_session_factory() as session:
        lease = await session.scalar(select(OrchestrationEnvironmentLease))
        lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    expired = await _acquire_committed(pg_session_factory, facade, successor)
    assert expired.kind is LeaseOutcomeKind.CONFLICT
    async with pg_session_factory() as session:
        recorded = await reconcile_lease(
            session,
            canonical_target_key=gateway.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=owner,
        )
        assert recorded.applied
        await session.commit()
    handed_off = await _acquire_committed(pg_session_factory, facade, successor)
    assert handed_off.applied
    async with pg_session_factory() as session:
        stale = await release_lease(
            session,
            canonical_target_key=gateway.canonical_key,
            holder=owner,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )
        assert stale.kind is LeaseOutcomeKind.CONFLICT
    record_property(
        "foundation_observation",
        json.dumps(
            {
                "competing": competing.kind.value,
                "expired": expired.kind.value,
                "stale": stale.kind.value,
                "successor_applied": handed_off.applied,
                "fixture_one": isolated_release_fixture(gateway, owner="phase-one"),
                "fixture_two": isolated_release_fixture(facade, owner="phase-two"),
            }
        ),
    )


def test_shared_release_scope_rejects_unreviewed_or_ambiguous_names():
    from src.orchestration.environment_leases import isolated_release_fixture, shared_release_target

    with pytest.raises(LeaseError, match="reviewed group"):
        shared_release_target(_target(), environment="dev/other", group="tool-connectors")
    with pytest.raises(LeaseError, match="release group"):
        isolated_release_fixture(_target(), owner="phase-one")


async def test_foundation_receipt_requires_current_scoped_owner_and_pinned_source(pg_session_factory):
    from src.orchestration.environment_leases import shared_release_held, shared_release_target

    group = shared_release_target(_target(), environment="dev", group="atlassian-foundation")
    prior = _holder(ORG_B, "foreign-stage")
    current = _holder(ORG_A, "foundation-stage")

    async def held(session, *, release_ref="b" * 40, manifest_entry_id=ENTRY, generation=1, started_at=None):
        return await shared_release_held(
            session,
            target=group,
            org_id=ORG_A,
            action_id=current.action_id,
            release_ref=release_ref,
            manifest_entry_id=manifest_entry_id,
            generation=generation,
            started_at=started_at or datetime.now(UTC),
        )

    assert (await _acquire_committed(pg_session_factory, group, prior, release_ref="a" * 40)).applied
    async with pg_session_factory() as session:
        assert not await held(session, release_ref="a" * 40)
    async with pg_session_factory() as session:
        assert (
            await release_lease(
                session,
                canonical_target_key=group.canonical_key,
                holder=prior,
                reason=ReleaseReason.COMPLETED,
                terminal_evidence="prior stage evaluated",
            )
        ).applied
        await session.commit()
    assert (await _acquire_committed(pg_session_factory, group, current, release_ref="b" * 40)).applied
    async with pg_session_factory() as session:
        assert not await held(session, started_at=datetime.now(UTC) + timedelta(minutes=1))
    created_before_acquisition = datetime.now(UTC) - timedelta(minutes=1)
    async with pg_session_factory() as session:
        assert not await held(session, release_ref="a" * 40)
        assert not await held(session, manifest_entry_id="different")
        assert not await held(session, generation=2)
        assert not await held(session, started_at=created_before_acquisition)
        assert await held(session)
    async with pg_session_factory() as session:
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == group.canonical_key)
        )
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    async with pg_session_factory() as session:
        assert not await held(session)


async def test_foundation_components_and_revisions_compete_for_one_target(pg_session_factory):
    from types import SimpleNamespace

    from src.orchestration.environment_leases import foundation_release_scope, isolated_release_fixture

    flow = SimpleNamespace(id="release-flow", org_id=ORG_A, slug="atlassian-adapters-8445-parallel")
    node = SimpleNamespace(id="release-node", flow_id=flow.id, org_id=ORG_A, wave_ref="w03b-foundation-release")
    entry = SimpleNamespace(release_group="atlassian-foundation", covers=lambda component: component in {"gateway-backend", "tool-adapters"})

    def scoped(component, revision):
        return foundation_release_scope(
            node=node, flow=flow, entry=entry, component=component, target=_target(), environment="dev", source_revision=revision
        )

    target, gateway_owner = scoped("gateway-backend", "a" * 40)
    _, adapter_owner = scoped("tool-adapters", "a" * 40)
    _, changed_owner = scoped("gateway-backend", "b" * 40)
    assert len({gateway_owner, adapter_owner, changed_owner}) == 3
    assert isolated_release_fixture(target, owner=gateway_owner) != isolated_release_fixture(target, owner=adapter_owner)
    assert (await _acquire_committed(pg_session_factory, target, _holder(ORG_A, gateway_owner), release_ref="a" * 40)).applied
    for owner, revision in ((adapter_owner, "a" * 40), (changed_owner, "b" * 40)):
        refused = await _acquire_committed(pg_session_factory, target, _holder(ORG_A, owner), release_ref=revision)
        assert refused.kind is LeaseOutcomeKind.CONFLICT
    assert (await _fetch(pg_session_factory, target.canonical_key)).release_ref == "a" * 40


async def test_shared_receipt_refuses_cached_owner_after_committed_handoff(pg_session_factory):
    from src.orchestration.environment_leases import shared_release_held, shared_release_target

    group = shared_release_target(_target(), environment="dev", group="atlassian-foundation")
    first = _holder(ORG_A, "release-a")
    second = _holder(ORG_B, "release-b")
    assert (await _acquire_committed(pg_session_factory, group, first, release_ref="a" * 40)).applied
    async with pg_session_factory() as reader:
        cached = await reader.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == group.canonical_key)
        )
        assert cached.owner_action_id == first.action_id
        async with pg_session_factory() as writer:
            released = await release_lease(
                writer,
                canonical_target_key=group.canonical_key,
                holder=first,
                reason=ReleaseReason.COMPLETED,
                terminal_evidence="first release completed",
            )
            assert released.applied
            await writer.commit()
        assert (await _acquire_committed(pg_session_factory, group, second, release_ref="b" * 40)).applied
        assert not await shared_release_held(
            reader,
            target=group,
            org_id=ORG_A,
            action_id=first.action_id,
            manifest_entry_id=ENTRY,
            release_ref="a" * 40,
            generation=1,
            started_at=datetime.now(UTC),
        )
        assert cached.owner_action_id == second.action_id


async def test_same_release_renewal_preserves_dispatch_start_fence(pg_session_factory):
    from src.orchestration.environment_leases import shared_release_held, shared_release_target

    group = shared_release_target(_target(), environment="dev", group="atlassian-foundation")
    holder = _holder(ORG_A, "release-a")
    first = await _acquire_committed(pg_session_factory, group, holder, release_ref="a" * 40)
    assert first.applied
    run_created_at = datetime.now(UTC)
    renewed = await _acquire_committed(pg_session_factory, group, holder, release_ref="a" * 40)
    assert renewed.applied and renewed.lease.acquired_at == first.lease.acquired_at
    async with pg_session_factory() as session:
        assert await shared_release_held(
            session,
            target=group,
            org_id=ORG_A,
            action_id=holder.action_id,
            release_ref="a" * 40,
            manifest_entry_id=ENTRY,
            generation=1,
            started_at=run_created_at,
        )


async def test_shared_release_handoff_is_atomic_against_competing_releases(pg_session_factory, monkeypatch):
    import src.orchestration.environment_leases as leases

    group = leases.shared_release_target(_target(), environment="dev", group="atlassian-foundation")
    gateway = _holder(ORG_A, "gateway-release")
    adapter = _holder(ORG_A, "adapter-release")
    contender = _holder(ORG_B, "other-release")
    assert (await _acquire_committed(pg_session_factory, group, gateway, release_ref="a" * 40)).applied
    reached_handoff = asyncio.Event()
    permit_successor = asyncio.Event()
    contender_started = asyncio.Event()
    original_acquire = leases.acquire_lease

    async def pause_successor(session, *, holder, **kwargs):
        if holder.action_id == adapter.action_id:
            reached_handoff.set()
            await asyncio.wait_for(permit_successor.wait(), timeout=5)
        return await original_acquire(session, holder=holder, **kwargs)

    async def compete():
        contender_started.set()
        return await _acquire_committed(pg_session_factory, group, contender, release_ref="b" * 40)

    monkeypatch.setattr(leases, "acquire_lease", pause_successor)
    async with pg_session_factory() as session:
        handoff = asyncio.create_task(
            leases.handoff_shared_release(
                session,
                target=group,
                holder=gateway,
                successor=adapter,
                current_entry_id=ENTRY,
                next_entry_id="adapter-entry",
                current_revision="a" * 40,
                next_revision="a" * 40,
                terminal_evidence="verified gateway receipt",
            )
        )
        await asyncio.wait_for(reached_handoff.wait(), timeout=5)
        competing = asyncio.create_task(compete())
        await asyncio.wait_for(contender_started.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not competing.done()
        permit_successor.set()
        outcome = await asyncio.wait_for(handoff, timeout=5)
        assert outcome.applied and outcome.lease.owner_action_id == adapter.action_id
        await session.commit()
        refused = await asyncio.wait_for(competing, timeout=5)
        assert refused.kind is LeaseOutcomeKind.CONFLICT
    async with pg_session_factory() as session:
        stale = await leases.handoff_shared_release(
            session,
            target=group,
            holder=gateway,
            successor=_holder(ORG_A, "late-release"),
            current_entry_id=ENTRY,
            next_entry_id="contender-entry",
            current_revision="a" * 40,
            next_revision="b" * 40,
            terminal_evidence="late predecessor receipt",
        )
        assert stale.kind is LeaseOutcomeKind.CONFLICT and stale.lease is None
    row = await _fetch(pg_session_factory, group.canonical_key)
    assert row.state == LeaseState.HELD.value and row.owner_action_id == adapter.action_id


async def test_expired_shared_release_refuses_handoff_until_owner_renews(pg_session_factory):
    from src.orchestration.environment_leases import handoff_shared_release, shared_release_target

    group = shared_release_target(_target(), environment="dev", group="atlassian-foundation")
    owner = _holder(ORG_A, "gateway-release")
    successor = _holder(ORG_A, "adapter-release")
    assert (await _acquire_committed(pg_session_factory, group, owner, release_ref="a" * 40)).applied
    async with pg_session_factory() as session:
        lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == group.canonical_key)
        )
        lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    async with pg_session_factory() as session:
        refused = await handoff_shared_release(
            session,
            target=group,
            holder=owner,
            successor=successor,
            current_entry_id=ENTRY,
            next_entry_id="adapter-entry",
            current_revision="a" * 40,
            next_revision="b" * 40,
            terminal_evidence="verified gateway completion",
        )
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        await session.commit()
    unchanged = await _fetch(pg_session_factory, group.canonical_key)
    assert unchanged.owner_action_id == owner.action_id and unchanged.state == LeaseState.HELD.value
    assert (await _acquire_committed(pg_session_factory, group, owner, release_ref="a" * 40)).applied
    async with pg_session_factory() as session:
        admitted = await handoff_shared_release(
            session,
            target=group,
            holder=owner,
            successor=successor,
            current_entry_id=ENTRY,
            next_entry_id="adapter-entry",
            current_revision="a" * 40,
            next_revision="b" * 40,
            terminal_evidence="verified gateway completion",
        )
        assert admitted.applied
        await session.commit()
    updated = await _fetch(pg_session_factory, group.canonical_key)
    assert updated.owner_action_id == successor.action_id and updated.state == LeaseState.HELD.value


async def test_shared_release_handoff_rollback_keeps_prior_holder(pg_session_factory, monkeypatch):
    import src.orchestration.environment_leases as leases

    group = leases.shared_release_target(_target(), environment="dev", group="atlassian-foundation")
    first = _holder(ORG_A, "gateway-release")
    second = _holder(ORG_A, "adapter-release")
    assert (await _acquire_committed(pg_session_factory, group, first, release_ref="a" * 40)).applied

    async def failed_next_acquisition(*args, **kwargs):
        raise RuntimeError("second mutation failed")

    monkeypatch.setattr(leases, "acquire_lease", failed_next_acquisition)
    async with pg_session_factory() as session:
        with pytest.raises(RuntimeError, match="second mutation failed"):
            await leases.handoff_shared_release(
                session,
                target=group,
                holder=first,
                successor=second,
                current_entry_id=ENTRY,
                next_entry_id="adapter-entry",
                current_revision="a" * 40,
                next_revision="a" * 40,
                terminal_evidence="verified gateway receipt",
            )
        await session.commit()
    row = await _fetch(pg_session_factory, group.canonical_key)
    assert row.state == LeaseState.HELD.value and row.owner_action_id == first.action_id


async def test_unsettled_publication_survives_restart_and_blocks_terminal_handoff(pg_session_factory):
    from src.orchestration.environment_leases import shared_release_target

    target = shared_release_target(_target(), environment="dev", group="adapter-foundation")
    first = _holder(ORG_A, "native-run")
    source = "a" * 40
    digest = "sha256:" + "b" * 64
    assert (await _acquire_committed(pg_session_factory, target, first, release_ref=source)).applied
    async with pg_session_factory() as session:
        assert await begin_shared_publication(
            session,
            target=target,
            holder=first,
            manifest_entry_id=ENTRY,
            release_ref=source,
            digest=digest,
        )
        await session.commit()
    async with pg_session_factory() as session:
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
        )
        assert row.publication_pending_digest == digest
        row.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        row.reconciled_terminal_evidence = EVIDENCE_TEXT
        await session.commit()
    async with pg_session_factory() as session:
        contender = await acquire_lease(
            session,
            target=target,
            holder=_holder(ORG_B, "next-release"),
            manifest_entry_id=ENTRY,
            release_ref="c" * 40,
        )
        assert contender.kind is LeaseOutcomeKind.CONFLICT and contender.lease is None
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
        )
        assert row.publication_pending_digest == digest
        assert row.owner_action_id == first.action_id
    image = ImageReadback(target.account_id, target.region, "adp-tools-dev-adapters", source, digest, datetime.now(UTC))
    async with pg_session_factory() as session:
        assert not await recover_shared_publication(
            session,
            target=target,
            holder=first,
            manifest_entry_id=ENTRY,
            release_ref=source,
            digest=digest,
            terminal_evidence=EVIDENCE_TEXT,
            image_readback=ImageReadback(target.account_id, target.region, image.repository, source, "sha256:" + "c" * 64, image.observed_at),
        )
        await session.commit()
    assert (await _fetch(pg_session_factory, target.canonical_key)).publication_pending_digest == digest
    async with pg_session_factory() as session:
        assert await recover_shared_publication(
            session,
            target=target,
            holder=first,
            manifest_entry_id=ENTRY,
            release_ref=source,
            digest=digest,
            terminal_evidence=EVIDENCE_TEXT,
            image_readback=image,
        )
        await session.commit()
    row = await _fetch(pg_session_factory, target.canonical_key)
    assert row.publication_pending_digest is None and row.reconciled_terminal_evidence == EVIDENCE_TEXT
    assert (await _acquire_committed(pg_session_factory, target, _holder(ORG_B, "next-release"), release_ref="c" * 40)).applied


async def test_restart_reacquires_only_original_release_without_clearing_pending_image(pg_session_factory):
    from src.orchestration.environment_leases import shared_release_target

    target = shared_release_target(_target(), environment="dev", group="adapter-foundation")
    first = _holder(ORG_A, "native-run")
    revision = "a" * 40
    digest = "sha256:" + "b" * 64
    assert (await _acquire_committed(pg_session_factory, target, first, release_ref=revision)).applied
    async with pg_session_factory() as session:
        assert await begin_shared_publication(session, target=target, holder=first, manifest_entry_id=ENTRY, release_ref=revision, digest=digest)
        await session.commit()
    async with pg_session_factory() as session:
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
        )
        row.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    async with pg_session_factory() as session:
        changed_release = await acquire_lease(session, target=target, holder=first, manifest_entry_id=ENTRY, release_ref="c" * 40)
        assert changed_release.kind is LeaseOutcomeKind.CONFLICT
        renewed = await acquire_lease(session, target=target, holder=first, manifest_entry_id=ENTRY, release_ref=revision)
        assert renewed.applied
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
        )
        assert row.publication_pending_digest == digest
        assert row.reconciled_terminal_evidence is None
        await session.commit()
    async with pg_session_factory() as session:
        contender = await acquire_lease(session, target=target, holder=_holder(ORG_B, "next-release"), manifest_entry_id=ENTRY, release_ref="c" * 40)
        assert contender.kind is LeaseOutcomeKind.CONFLICT and contender.lease is None
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
        )
        assert row.publication_pending_digest == digest


async def test_terminal_absence_does_not_let_a_competing_postgres_session_take_over(pg_session_factory):
    from src.orchestration.environment_leases import shared_release_target

    target = shared_release_target(_target(), environment="dev", group="adapter-foundation")
    holder = _holder(ORG_A, "native-run")
    source = "a" * 40
    digest = "sha256:" + "b" * 64
    assert (await _acquire_committed(pg_session_factory, target, holder, release_ref=source)).applied
    async with pg_session_factory() as session:
        assert await begin_shared_publication(session, target=target, holder=holder, manifest_entry_id=ENTRY, release_ref=source, digest=digest)
        await session.commit()
    absence = ImageAbsence(target.account_id, target.region, "adp-tools-dev-adapters", source, digest, datetime.now(UTC))
    async with pg_session_factory() as session:
        assert await record_shared_publication_absence(
            session,
            target=target,
            holder=holder,
            manifest_entry_id=ENTRY,
            release_ref=source,
            digest=digest,
            terminal_evidence=EVIDENCE_TEXT,
            image_absence=absence,
        )
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
        )
        row.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    async with pg_session_factory() as session:
        refreshed = await acquire_lease(session, target=target, holder=holder, manifest_entry_id=ENTRY, release_ref=source)
        assert refreshed.applied
        displaced = await acquire_lease(session, target=target, holder=_holder(ORG_A, "native-run", 2), manifest_entry_id=ENTRY, release_ref=source)
        assert displaced.kind is LeaseOutcomeKind.CONFLICT
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
        )
        row.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    async with pg_session_factory() as session:
        contender = await acquire_lease(session, target=target, holder=_holder(ORG_B, "next"), manifest_entry_id=ENTRY, release_ref="c" * 40)
        assert contender.kind is LeaseOutcomeKind.CONFLICT and contender.lease is None
        row = await session.scalar(
            select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
        )
        assert row.publication_pending_digest == digest
        assert row.reconciled_terminal_evidence == EVIDENCE_TEXT
