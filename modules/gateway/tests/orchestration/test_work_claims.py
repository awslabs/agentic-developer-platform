"""Tests for shared issue ownership — the work-claim admission service (#5127).

These carry the story's acceptance criteria A0-1 … A0-5. What is asserted here is
*semantics*: which disposition each situation produces and which state the row is
left in. The concurrency guarantee itself is asserted separately, against a real
PostgreSQL server, in `tests/orchestration/test_work_claims_postgres.py` — SQLite
treats `SELECT ... FOR UPDATE` as a no-op, so a locking assertion here would pass
without testing any locking at all. Keeping the two files apart is deliberate:
the story explicitly requires PostgreSQL concurrency tests rather than
"SQLite-only locking assertions", and a green SQLite run must not be mistakable
for evidence about locking.

Several tests here are **negative** — they prove a guard was implemented rather
than worked around:

- `TestLeaseExpiryIsNotTakeover` (A0-3): an expired lease alone never admits a
  second owner. This is the tempting shortcut the design forbids.
- `TestForcedHandover` (A0-3/A0-4): `live` and `unverifiable` both block, a
  faulted liveness lookup blocks, and an unattested credential state blocks.
- `TestStaleGeneration` (A0-4): a superseded generation cannot bind a run,
  refresh a lease, or release the replacement's claim.
- `TestBindingRequiresImmutableRepositoryId`: a repo *name* cannot be laundered
  into the binding, and a falsy id is refused rather than defaulted to 0.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.models import ClaimState, OrchestrationWorkClaim
from src.orchestration.work_claims import (
    ClaimBinding,
    ClaimOwner,
    Disposition,
    OwnerKind,
    ReleaseReason,
    WorkClaimError,
    bind_run,
    claim_work,
    force_handover,
    heartbeat,
    release_work,
)
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
REPO_ID = 987_654_321
OTHER_REPO_ID = 123_456_789
ISSUE = 5127


@pytest.mark.parametrize("operation", ["bind", "heartbeat", "release", "handover"])
async def test_claim_id_never_grants_cross_tenant_access(session, operation):
    receipt = await claim_work(
        session, binding=ClaimBinding(ORG_A, REPO_ID, ISSUE), owner=ClaimOwner(OwnerKind.ENGINE_FLOW, "flow-a"), event_id="owner-event"
    )
    kwargs = {"org_id": ORG_B, "claim_id": receipt.claim_id, "generation": receipt.generation}
    with pytest.raises(WorkClaimError, match="does not exist"):
        if operation == "bind":
            await bind_run(session, **kwargs, run_id="attacker")
        elif operation == "heartbeat":
            await heartbeat(session, **kwargs)
        elif operation == "release":
            await release_work(session, **kwargs, reason=ReleaseReason.COMPLETED, terminal_evidence="forged")
        else:
            kwargs.pop("generation")
            await force_handover(session, **kwargs, decision_id="forged", resolver=None, effects_reconciled=True, credentials_reconciled=True)
    row = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert row.state == ClaimState.HELD.value
    assert row.generation == 1
    assert row.active_run_id is None


# ---------------------------------------------------------------------------
# Fixtures — same SQLite-in-memory shape as test_dispatch_pass.py
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


class FakeLivenessResolver:
    """Stands in for the DynamoDB-backed `RunBindingResolver`.

    Returns whatever ingress row a test wants, or raises, so the handover guards
    can be driven through every verdict without AWS.
    """

    def __init__(self, rows: dict[str, dict] | None = None, *, fault: bool = False) -> None:
        self.rows = rows or {}
        self.fault = fault
        self.calls: list[str] = []

    async def resolve(self, run_id: str) -> dict | None:
        self.calls.append(run_id)
        if self.fault:
            raise RuntimeError("simulated DynamoDB fault")
        return self.rows.get(run_id)


def _binding(org: str = ORG_A, repo_id: int = REPO_ID, issue: int = ISSUE) -> ClaimBinding:
    return ClaimBinding(org_id=org, provider_repository_id=repo_id, issue_number=issue)


def _engine_owner(ref: str = "flow-abc/wave-1") -> ClaimOwner:
    return ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref=ref)


def _direct_owner(ref: str = "webhook:developer") -> ClaimOwner:
    return ClaimOwner(kind=OwnerKind.DIRECT_DISPATCH, ref=ref)


def _live_row(run_id: str = "run-1") -> dict:
    """An ingress row that `compute_liveness` reads as `live`."""
    return {
        "event_id": run_id,
        "status": "in_progress",
        "arrived_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status_updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _exited_row(run_id: str = "run-1") -> dict:
    """An ingress row carrying positive evidence of an exit."""
    return {
        "event_id": run_id,
        "status": "complete",
        "arrived_at": "2026-09-01T00:00:00Z",
        "status_updated_at": "2026-09-01T01:00:00Z",
    }


def _unverifiable_row(run_id: str = "run-1") -> dict:
    """Active status, but the last signal is far outside the staleness window.

    This is what a worker partitioned from the platform looks like — and what a
    long-running worker whose status writer stalled looks like. Neither is an exit.
    """
    return {
        "event_id": run_id,
        "status": "in_progress",
        "arrived_at": "2026-01-01T00:00:00Z",
        "status_updated_at": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# A0-1: simultaneous engine/direct claims admit one run
# ---------------------------------------------------------------------------


class TestSingleAdmission:
    """A0-1. One issue, two launch paths, exactly one admitted execution."""

    async def test_first_claim_is_admitted(self, session):
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")

        assert receipt.disposition is Disposition.ADMITTED
        assert receipt.admitted is True
        assert receipt.generation == 1
        assert receipt.claim_id

    async def test_engine_and_direct_dispatch_produce_one_admission_and_one_scoped_refusal(self, session):
        """The core requirement: both paths claim the same issue, one wins.

        The refusal names the holder so an operator can find the other run, and it
        is a `CONFLICT` rather than an error — the second path behaved correctly,
        it simply lost.
        """
        first = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-engine")
        second = await claim_work(session, binding=_binding(), owner=_direct_owner(), event_id="evt-webhook")

        assert first.disposition is Disposition.ADMITTED
        assert second.disposition is Disposition.CONFLICT
        assert second.admitted is False
        assert second.reason == "held_by_other_owner"
        assert second.holder_ref == "flow-abc/wave-1"

        # And exactly one row owns the issue.
        rows = (await session.execute(select(OrchestrationWorkClaim))).scalars().all()
        assert len(rows) == 1
        assert rows[0].owner_kind == OwnerKind.ENGINE_FLOW.value

    async def test_direct_dispatch_first_also_wins(self, session):
        """Symmetry: neither path is privileged. Whoever arrives first owns it."""
        first = await claim_work(session, binding=_binding(), owner=_direct_owner(), event_id="evt-webhook")
        second = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-engine")

        assert first.disposition is Disposition.ADMITTED
        assert second.disposition is Disposition.CONFLICT

    async def test_duplicate_event_returns_the_original_receipt(self, session):
        """A0-1. At-least-once delivery must not become two runs.

        The replay gets `DUPLICATE` with the *same* claim id and generation, so a
        caller that lost its response can recover its receipt — but `admitted` is
        false, so it cannot read the receipt as permission to start a second run.
        """
        first = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        replay = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")

        assert replay.disposition is Disposition.DUPLICATE
        assert replay.admitted is False
        assert replay.claim_id == first.claim_id
        assert replay.generation == first.generation

    async def test_duplicate_check_precedes_the_ownership_check(self, session):
        """A redelivery is a duplicate even when the claim has since been released.

        Ordering matters: if ownership were tested first, a retry of an event we
        already honored would be admitted *again* against the released row and
        advance the generation, producing a second run from one event.
        """
        first = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        await release_work(
            session,
            org_id=ORG_A,
            claim_id=first.claim_id,
            generation=first.generation,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="status=complete",
        )

        replay = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")

        assert replay.disposition is Disposition.DUPLICATE
        assert replay.generation == first.generation

    async def test_missing_event_id_is_refused_not_defaulted(self, session):
        """Without an event id, idempotency would silently not exist."""
        with pytest.raises(WorkClaimError) as exc:
            await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="   ")
        assert exc.value.code == "missing_event_id"


# ---------------------------------------------------------------------------
# A0-2: repeated issue references and tenant isolation
# ---------------------------------------------------------------------------


class TestScopeIsolation:
    """A0-2. Ownership cannot be evaded, and does not over-refuse."""

    async def test_separate_issues_progress_independently(self, session):
        a = await claim_work(session, binding=_binding(issue=100), owner=_engine_owner(), event_id="evt-a")
        b = await claim_work(session, binding=_binding(issue=101), owner=_engine_owner(), event_id="evt-b")

        assert a.disposition is Disposition.ADMITTED
        assert b.disposition is Disposition.ADMITTED
        assert a.claim_id != b.claim_id

    async def test_same_issue_number_in_different_tenants_is_independent(self, session):
        """Overbroad refusal is a listed failure mode: one tenant's claim must not
        block another's identically-numbered issue."""
        a = await claim_work(session, binding=_binding(org=ORG_A), owner=_engine_owner(), event_id="evt-a")
        b = await claim_work(session, binding=_binding(org=ORG_B), owner=_engine_owner(), event_id="evt-b")

        assert a.disposition is Disposition.ADMITTED
        assert b.disposition is Disposition.ADMITTED

    async def test_same_issue_number_in_different_repositories_is_independent(self, session):
        """Issue 5127 in two repositories is two pieces of work, not one."""
        a = await claim_work(session, binding=_binding(repo_id=REPO_ID), owner=_engine_owner(), event_id="evt-a")
        b = await claim_work(session, binding=_binding(repo_id=OTHER_REPO_ID), owner=_engine_owner(), event_id="evt-b")

        assert a.disposition is Disposition.ADMITTED
        assert b.disposition is Disposition.ADMITTED

    async def test_repeated_issue_reference_across_plans_cannot_evade_ownership(self, session):
        """A0-2. Two different flows/lanes naming the same issue: one is refused.

        This is the plan-level shape of the bug — an issue referenced twice inside
        a plan, or by two plans, must not produce two runs just because the
        *referrers* differ. Ownership is keyed on the issue, not on the referrer.
        """
        first = await claim_work(session, binding=_binding(), owner=_engine_owner("flow-one/wave-1"), event_id="evt-1")
        second = await claim_work(session, binding=_binding(), owner=_engine_owner("flow-two/wave-3"), event_id="evt-2")

        assert first.disposition is Disposition.ADMITTED
        assert second.disposition is Disposition.CONFLICT
        assert second.holder_ref == "flow-one/wave-1"


class TestBindingRequiresImmutableRepositoryId:
    """The binding cannot be built from a mutable repository name.

    The whole reason the claim is keyed on the provider's numeric id is that a
    rename must not bypass ownership. These assert that the type system and the
    validator actually enforce it, rather than the docstring merely recommending
    it.
    """

    @pytest.mark.parametrize("bad", ["aws-e/adp", "987654321", None, 0, -1, True])
    def test_non_positive_or_non_integer_repository_id_is_refused(self, bad):
        with pytest.raises(WorkClaimError) as exc:
            ClaimBinding(org_id=ORG_A, provider_repository_id=bad, issue_number=ISSUE)
        assert exc.value.code == "invalid_binding"

    @pytest.mark.parametrize("bad", [0, -3, "5127", None, True])
    def test_non_positive_or_non_integer_issue_number_is_refused(self, bad):
        with pytest.raises(WorkClaimError) as exc:
            ClaimBinding(org_id=ORG_A, provider_repository_id=REPO_ID, issue_number=bad)
        assert exc.value.code == "invalid_binding"

    def test_empty_tenant_is_refused(self):
        with pytest.raises(WorkClaimError) as exc:
            ClaimBinding(org_id="  ", provider_repository_id=REPO_ID, issue_number=ISSUE)
        assert exc.value.code == "invalid_binding"

    def test_empty_owner_ref_is_refused(self):
        with pytest.raises(WorkClaimError) as exc:
            ClaimOwner(kind=OwnerKind.ENGINE_FLOW, ref="")
        assert exc.value.code == "invalid_owner"


# ---------------------------------------------------------------------------
# A0-3: lease expiry, liveness, and forced handover
# ---------------------------------------------------------------------------


class TestLeaseExpiryIsNotTakeover:
    """A0-3. The central negative property of the whole design.

    "The lease expired, so the previous worker must be gone" is false exactly when
    it is dangerous: a worker partitioned from the database is still pushing
    commits. If these tests ever pass by admitting the second claim, the story's
    protection is gone even though every other test still reads green.
    """

    async def test_expired_lease_alone_does_not_admit_a_second_owner(self, session):
        first = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1", lease_seconds=60)

        # Age the lease past expiry without any exit evidence.
        claim = await session.get(OrchestrationWorkClaim, first.claim_id)
        claim.lease_expires_at = datetime.now(UTC) - timedelta(hours=2)
        await session.flush()

        second = await claim_work(session, binding=_binding(), owner=_direct_owner(), event_id="evt-2")

        assert second.disposition is Disposition.CONFLICT
        assert second.admitted is False
        # The reason distinguishes a lapsed lease from a healthy holder, so an
        # operator can see there is something to reconcile — without the refusal
        # itself weakening.
        assert second.reason == "held_lease_lapsed"

    async def test_naive_stored_lease_timestamp_does_not_break_admission(self, session):
        """Regression: comparing a naive stored timestamp to an aware `now` raised
        `TypeError` inside `claim_work`.

        This failed in the worst available direction. The crash happened *at
        admission*, and a caller that cannot get an answer about ownership must
        fail closed — so one unlabelled timestamp stopped dispatch for the whole
        tenant rather than merely mis-reporting a lease. Drivers that drop the UTC
        offset (SQLite here, but any such driver) produce exactly this value.
        """
        first = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        claim = await session.get(OrchestrationWorkClaim, first.claim_id)
        # Explicitly naive, as a driver without offset support hands back.
        claim.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        await session.flush()

        second = await claim_work(session, binding=_binding(), owner=_direct_owner(), event_id="evt-2")

        assert second.disposition is Disposition.CONFLICT
        assert second.reason == "held_lease_lapsed"

    async def test_claim_stays_held_after_lease_expiry(self, session):
        """Expiry changes no state. Only an explicit release or handover does."""
        first = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        claim = await session.get(OrchestrationWorkClaim, first.claim_id)
        claim.lease_expires_at = datetime.now(UTC) - timedelta(hours=2)
        await session.flush()

        await claim_work(session, binding=_binding(), owner=_direct_owner(), event_id="evt-2")

        await session.refresh(claim)
        assert claim.state == ClaimState.HELD.value
        assert claim.generation == 1


class TestForcedHandover:
    """A0-3. Handover needs proven exit AND reconciliation. Each guard tested alone."""

    async def _held_claim_with_run(self, session, run_id="run-1"):
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id=run_id)
        return receipt

    async def test_live_run_blocks_handover(self, session):
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({"run-1": _live_row()})

        result = await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "run_live"

    async def test_unverifiable_run_blocks_handover(self, session):
        """The important one. Loss of contact is not evidence of an exit."""
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({"run-1": _unverifiable_row()})

        result = await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "run_unverifiable"

    async def test_faulted_liveness_lookup_blocks_handover(self, session):
        """Refuse, do not degrade: an unreadable liveness record is exactly when
        assuming an exit is unsafe."""
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver(fault=True)

        result = await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "liveness_unavailable"

    async def test_absent_ingress_row_blocks_handover(self, session):
        """Absence of a record is not evidence of absence — the row may simply not
        have been written yet."""
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({})

        result = await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "liveness_unknown"

    async def test_unreconciled_effects_block_even_with_proven_exit(self, session):
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({"run-1": _exited_row()})

        result = await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=False,
            credentials_reconciled=True,
        )

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "effects_not_reconciled"

    async def test_unreconciled_credentials_block_even_with_proven_exit(self, session):
        """A0-4's credential arm. No database row can revoke an already-issued
        GitHub token, so an unattested credential state must block rather than let
        the code advertise a fence it does not have."""
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({"run-1": _exited_row()})

        result = await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=False,
        )

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "credentials_not_reconciled"

    async def test_missing_decision_is_refused(self, session):
        """Handover is an operator action; a caller cannot grant itself one."""
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({"run-1": _exited_row()})

        with pytest.raises(WorkClaimError) as exc:
            await force_handover(
                session,
                org_id=ORG_A,
                claim_id=receipt.claim_id,
                decision_id="",
                resolver=resolver,
                effects_reconciled=True,
                credentials_reconciled=True,
            )
        assert exc.value.code == "missing_decision"

    async def test_proven_exit_and_reconciliation_advances_the_generation(self, session):
        """A0-3's positive arm: with every guard satisfied, handover proceeds."""
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({"run-1": _exited_row()})

        result = await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )

        assert result.disposition is Disposition.ADMITTED
        assert result.generation == receipt.generation + 1

        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        # Left RELEASED, not reassigned: the replacement goes through normal
        # admission rather than being spliced in by the handover.
        assert claim.state == ClaimState.RELEASED.value
        assert claim.release_reason == ReleaseReason.HANDOVER.value
        assert claim.active_run_id is None
        assert claim.lease_expires_at is None

    async def test_replacement_can_then_be_admitted(self, session):
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({"run-1": _exited_row()})
        await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )

        replacement = await claim_work(session, binding=_binding(), owner=_direct_owner(), event_id="evt-replacement")

        assert replacement.disposition is Disposition.ADMITTED
        assert replacement.generation == 3  # 1 claimed, 2 handover, 3 readmitted

    async def test_superseded_event_cannot_return_a_duplicate_receipt_after_handover(self, session):
        """The handover clears the event id, so a replay of the *superseded* event
        cannot claim the new generation's receipt as its own."""
        receipt = await self._held_claim_with_run(session)
        resolver = FakeLivenessResolver({"run-1": _exited_row()})
        await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )

        replay = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")

        # Admitted as a genuinely new generation rather than handed the old receipt.
        assert replay.disposition is Disposition.ADMITTED
        assert replay.generation == 3

    async def test_handover_of_an_unbound_claim_needs_no_liveness_call(self, session):
        """A claim admitted but never bound to a run has no worker to prove exited."""
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        resolver = FakeLivenessResolver({})

        result = await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )

        assert result.disposition is Disposition.ADMITTED
        assert resolver.calls == []


# ---------------------------------------------------------------------------
# A0-4: stale generation cannot start another worker or action
# ---------------------------------------------------------------------------


class TestStaleGeneration:
    """A0-4. A superseded generation is inert on every mutating path."""

    async def _handed_over(self, session):
        """A claim whose generation has advanced past what the old run holds."""
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id="run-old")
        resolver = FakeLivenessResolver({"run-old": _exited_row("run-old")})
        await force_handover(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            decision_id="decision-1",
            resolver=resolver,
            effects_reconciled=True,
            credentials_reconciled=True,
        )
        return receipt  # carries the now-stale generation

    async def test_stale_generation_cannot_bind_a_run(self, session):
        """The check that stops the old worker running beside its replacement."""
        stale = await self._handed_over(session)

        result = await bind_run(session, org_id=ORG_A, claim_id=stale.claim_id, generation=stale.generation, run_id="run-old-retry")

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "stale_generation"

    async def test_stale_generation_cannot_refresh_the_lease(self, session):
        """Otherwise a superseded worker keeps the row looking alive and starves
        the replacement."""
        stale = await self._handed_over(session)

        result = await heartbeat(session, org_id=ORG_A, claim_id=stale.claim_id, generation=stale.generation)

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "stale_generation"

    async def test_stale_generation_cannot_release_the_replacements_claim(self, session):
        """A stale release would hand the issue to whoever asks next while the
        current owner is still working."""
        stale = await self._handed_over(session)
        replacement = await claim_work(session, binding=_binding(), owner=_direct_owner(), event_id="evt-2")

        result = await release_work(
            session,
            org_id=ORG_A,
            claim_id=stale.claim_id,
            generation=stale.generation,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="status=complete",
        )

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "stale_generation"

        # The replacement still holds it.
        claim = await session.get(OrchestrationWorkClaim, replacement.claim_id)
        assert claim.state == ClaimState.HELD.value
        assert claim.generation == replacement.generation

    async def test_second_run_cannot_bind_a_current_generation_already_in_use(self, session):
        """One mutating run at a time, even when the generation is current."""
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        first = await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id="run-1")
        second = await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id="run-2")

        assert first.disposition is Disposition.ADMITTED
        assert second.disposition is Disposition.BLOCKED
        assert second.reason == "run_already_bound"
        assert second.holder_ref == "run-1"

    async def test_rebinding_the_same_run_is_idempotent(self, session):
        """A worker retrying its own startup must not lock itself out."""
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id="run-1")
        again = await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id="run-1")

        assert again.disposition is Disposition.ADMITTED

    async def test_unknown_claim_raises_rather_than_reading_as_free(self, session):
        with pytest.raises(WorkClaimError) as exc:
            await bind_run(session, org_id=ORG_A, claim_id="no-such-claim", generation=1, run_id="run-1")
        assert exc.value.code == "unknown_claim"

    async def test_unrecognised_claim_state_blocks_admission(self, session):
        """An unknown state is indeterminate, not free — same reasoning as
        `compute_liveness` refusing to read an unknown status as exited."""
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        claim.state = "some_future_state"
        await session.flush()

        result = await claim_work(session, binding=_binding(), owner=_direct_owner(), event_id="evt-2")

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "unrecognised_claim_state"


# ---------------------------------------------------------------------------
# A0-5: sequential developer / reviewer / repair runs remain valid
# ---------------------------------------------------------------------------


class TestSequentialPersonaRuns:
    """A0-5. Ownership must not turn into a permanent lock on the issue.

    The listed failure mode is "overbroad refusal … legitimate sequential runs are
    blocked". These are the tests that would catch an implementation that keyed
    the claim on the run instead of the lane.
    """

    async def test_developer_then_reviewer_then_repair_all_admitted_in_sequence(self, session):
        personas = ["developer", "reviewer", "repair"]
        generations = []

        for index, persona in enumerate(personas, start=1):
            receipt = await claim_work(
                session,
                binding=_binding(),
                owner=_direct_owner(f"lane:{persona}"),
                event_id=f"evt-{persona}",
            )
            assert receipt.disposition is Disposition.ADMITTED, f"{persona} was refused"

            bound = await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id=f"run-{persona}")
            assert bound.disposition is Disposition.ADMITTED

            released = await release_work(
                session,
                org_id=ORG_A,
                claim_id=receipt.claim_id,
                generation=receipt.generation,
                reason=ReleaseReason.COMPLETED,
                terminal_evidence=f"{persona} finished",
            )
            assert released.disposition is Disposition.ADMITTED
            generations.append(receipt.generation)
            assert receipt.generation == index

        # Ordered reuse: one row, monotonically advancing generations.
        assert generations == [1, 2, 3]
        rows = (await session.execute(select(OrchestrationWorkClaim))).scalars().all()
        assert len(rows) == 1

    async def test_a_completed_run_does_not_permanently_suppress_later_work(self, session):
        """An issue's earlier merged PR must not block later authorized personas."""
        first = await claim_work(session, binding=_binding(), owner=_direct_owner("lane:developer"), event_id="evt-1")
        await release_work(
            session,
            org_id=ORG_A,
            claim_id=first.claim_id,
            generation=first.generation,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="pr merged",
        )

        later = await claim_work(session, binding=_binding(), owner=_direct_owner("lane:reviewer"), event_id="evt-2")

        assert later.disposition is Disposition.ADMITTED

    async def test_release_preserves_history_rather_than_deleting_the_row(self, session):
        """A deleted row would lose the release reason and reset the generation —
        and a reset generation makes a stale worker's credential look current."""
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        await release_work(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            generation=receipt.generation,
            reason=ReleaseReason.FAILED,
            terminal_evidence="status=failed",
        )

        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        assert claim is not None
        assert claim.state == ClaimState.RELEASED.value
        assert claim.release_reason == ReleaseReason.FAILED.value
        assert claim.released_at is not None
        assert claim.active_run_id is None
        assert claim.lease_expires_at is None

    async def test_release_requires_terminal_evidence(self, session):
        """An unevidenced release is a lease lapse by another name, and this is the
        call that makes the issue re-claimable."""
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")

        with pytest.raises(WorkClaimError) as exc:
            await release_work(
                session,
                org_id=ORG_A,
                claim_id=receipt.claim_id,
                generation=receipt.generation,
                reason=ReleaseReason.COMPLETED,
                terminal_evidence="",
            )
        assert exc.value.code == "missing_terminal_evidence"

    async def test_repeated_release_is_a_duplicate_not_an_error(self, session):
        """A retried terminal callback is normal, not a fault."""
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        args = dict(claim_id=receipt.claim_id, generation=receipt.generation, reason=ReleaseReason.COMPLETED, terminal_evidence="done")

        first = await release_work(session, org_id=ORG_A, **args)
        second = await release_work(session, org_id=ORG_A, **args)

        assert first.disposition is Disposition.ADMITTED
        assert second.disposition is Disposition.DUPLICATE
        assert second.reason == "already_released"


class TestHeartbeat:
    """Lease extension, so a long legitimate run is not mistaken for lost contact."""

    async def test_heartbeat_extends_the_lease(self, session):
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1", lease_seconds=60)
        claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
        before = claim.lease_expires_at

        result = await heartbeat(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, lease_seconds=7_200)

        assert result.disposition is Disposition.ADMITTED
        await session.refresh(claim)
        assert claim.lease_expires_at > before

    async def test_heartbeat_on_a_released_claim_is_blocked(self, session):
        receipt = await claim_work(session, binding=_binding(), owner=_engine_owner(), event_id="evt-1")
        await release_work(
            session,
            org_id=ORG_A,
            claim_id=receipt.claim_id,
            generation=receipt.generation,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="done",
        )

        result = await heartbeat(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation)

        assert result.disposition is Disposition.BLOCKED
        assert result.reason == "claim_not_held"


class TestNoTransportRouteIsAdded:
    """The internal-plane guard must stay satisfied by construction (#4196).

    Agent pods can call every `/internal/v1/*` route with any method, so a
    transport for this service is a security-relevant design step rather than a
    convenience. This asserts the module ships no router — if someone adds one
    here, this fails alongside `test_internal_plane_guard.py` and the addition
    gets the review it needs.
    """

    def test_work_claims_module_exposes_no_router(self):
        from src.orchestration import work_claims

        assert not hasattr(work_claims, "router")

    def test_work_claims_imports_no_fastapi(self):
        import ast
        import inspect

        from src.orchestration import work_claims

        tree = ast.parse(inspect.getsource(work_claims))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert not any(name.startswith("fastapi") for name in imported), f"work_claims.py imports FastAPI: {sorted(imported)}"
