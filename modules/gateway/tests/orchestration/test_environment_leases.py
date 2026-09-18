"""Semantics of the environment lease store (#5150, ENGINE-D1).

What is asserted here is *meaning*: which outcome each situation produces, which
columns the row is left holding, and which refusals are typed and opaque rather
than silent or chatty. The **concurrency** guarantee is asserted separately against
a real PostgreSQL server in `test_environment_leases_postgres.py`, because SQLite
treats `SELECT ... FOR UPDATE` as a no-op — a locking assertion here would pass
without testing any locking at all. Keeping the two files apart is the same
deliberate split `test_work_claims.py` and `test_execution_store.py` make, and for
the same reason: a green SQLite run must not be mistakable for evidence about
locking.

Most tests here are **negative** — they prove a guard exists rather than that a
happy path works:

- `TestCrossTenantCollision`: two tenants' aliases for one cluster contend for one
  lease, and the loser's refusal names nothing about the holder.
- `TestExpiryIsNotAnExit`: a lapsed lease with no reconciled evidence blocks
  takeover. This is the guard most likely to be "simplified" into a timeout.
- `TestStaleGenerationCannotRelease`: a superseded actor cannot free the target its
  successor is deploying to.
- `TestTerminalVersusRetryable`: CONFLICT and STALE stay distinct, because a caller
  that retries its way past a lost lease is a displaced actor that kept acting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.deployment_manifest import ManifestError, PhysicalTarget, TargetEvidence
from src.orchestration.environment_leases import (
    DEFAULT_LEASE_SECONDS,
    LeaseError,
    LeaseHolder,
    LeaseOutcomeKind,
    LeaseState,
    ReleaseReason,
    acquire_lease,
    reconcile_lease,
    release_lease,
)
from src.orchestration.models import OrchestrationEnvironmentLease
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
ENTRY = "adp-dev-embark1-gateway"
EVIDENCE_TEXT = "workflow run 12345 concluded: success"


def _evidence(source: str = "verified-aws-connection:conn-1") -> TargetEvidence:
    return TargetEvidence(source=source, verified_at="2026-09-18T00:00:00+00:00", detail="sts:AssumeRole readback")


def _target(*, resource_id: str = "cluster-a/adp-gateway", source: str = "verified-aws-connection:conn-1") -> PhysicalTarget:
    """A physical target. `000000000000` is a placeholder, not a plausible account.

    The story forbids inventing account ids, and a fixture that looked like a real
    account could be copied into configuration by someone skimming.
    """
    return PhysicalTarget(
        provider="aws",
        account_id="000000000000",
        region="us-east-1",
        resource_kind="eks-namespace",
        resource_id=resource_id,
        evidence=_evidence(source),
    )


# ---------------------------------------------------------------------------
# Fixtures — SQLite in memory, same shape as test_execution_store.py
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    # pysqlite's implicit BEGIN swallows SAVEPOINTs, and `acquire_lease` depends on
    # a savepoint to isolate an IntegrityError from the caller's transaction.
    # Without these two hooks the nested block is a no-op and the duplicate-key
    # test would assert nothing.
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


def _holder(org: str = ORG_A, action: str = "action-1", generation: int = 1) -> LeaseHolder:
    return LeaseHolder(org_id=org, action_id=action, generation=generation)


async def _row(session: AsyncSession, key: str) -> OrchestrationEnvironmentLease:
    result = await session.execute(select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == key))
    return result.scalar_one()


async def _expire(session: AsyncSession, key: str, *, reconciled: str | None = None) -> None:
    """Age a lease past its contact window, optionally recording terminal evidence.

    Written as a direct column edit rather than by waiting: the point under test is
    the decision the store makes about a lapsed row, not the clock.
    """
    row = await _row(session, key)
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if reconciled is not None:
        row.reconciled_terminal_evidence = reconciled
        row.reconciled_at = datetime.now(UTC)
    await session.flush()


class TestHolderValidation:
    """A malformed holder is refused at construction, not at the database."""

    def test_blank_tenant_is_refused(self):
        with pytest.raises(LeaseError) as exc:
            LeaseHolder(org_id="  ", action_id="action-1", generation=1)
        assert exc.value.code == "invalid_holder"

    def test_blank_action_is_refused(self):
        with pytest.raises(LeaseError):
            LeaseHolder(org_id=ORG_A, action_id="", generation=1)

    @pytest.mark.parametrize("generation", [0, -1])
    def test_non_positive_generation_is_refused(self, generation):
        """Generation 0 would disable the authority fence silently.

        Every stored row starts at a generation a holder must meet or exceed; a
        holder at 0 could never be superseded because nothing is below it.
        """
        with pytest.raises(LeaseError):
            LeaseHolder(org_id=ORG_A, action_id="action-1", generation=generation)

    def test_boolean_generation_is_refused(self):
        """`True == 1` in Python, so a bare int check would accept it."""
        with pytest.raises(LeaseError):
            LeaseHolder(org_id=ORG_A, action_id="action-1", generation=True)

    def test_holder_is_frozen(self):
        holder = _holder()
        with pytest.raises(Exception):
            holder.generation = 9


class TestAcquire:
    """Taking a free target, and what the row is left holding."""

    async def test_first_acquire_succeeds_and_records_its_authority(self, session):
        target = _target()
        outcome = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        assert outcome.applied
        lease = outcome.lease
        assert lease.state is LeaseState.HELD
        assert lease.revision == 1
        assert lease.owner_org_id == ORG_A
        assert lease.owner_action_id == "action-1"
        assert lease.owner_generation == 1
        # The approval that authorized the hold, so a live deploy is traceable to it.
        assert lease.manifest_entry_id == ENTRY
        # The canonicalization evidence, so "why were these two the same place?" is
        # answerable later from the record rather than from logs that expire.
        assert lease.evidence_source == "verified-aws-connection:conn-1"
        assert lease.evidence_verified_at == datetime(2026, 9, 18, tzinfo=UTC)
        assert lease.lease_expires_at > datetime.now(UTC)
        assert lease.reconciled_terminal_evidence is None

    async def test_acquire_requires_a_manifest_entry(self, session):
        """An unattributed hold cannot be audited.

        An operator looking at a live deployment must be able to see which reviewed
        approval permitted it.
        """
        with pytest.raises(LeaseError) as exc:
            await acquire_lease(session, target=_target(), holder=_holder(), manifest_entry_id="  ")
        assert exc.value.code == "invalid_acquire"

    @pytest.mark.parametrize("seconds", [0, -30, True])
    async def test_non_positive_lease_window_is_refused(self, session, seconds):
        with pytest.raises(LeaseError):
            await acquire_lease(
                session,
                target=_target(),
                holder=_holder(),
                manifest_entry_id=ENTRY,
                lease_seconds=seconds,
            )

    async def test_unparseable_evidence_time_is_refused_not_defaulted(self, session):
        """Defaulting to "now" would stamp storage time as verification time.

        That is a false provenance claim on the exact field an operator consults to
        judge whether a readback is stale.
        """
        target = PhysicalTarget(
            provider="aws",
            account_id="000000000000",
            region="us-east-1",
            resource_kind="eks-namespace",
            resource_id="cluster-a/adp-gateway",
            evidence=TargetEvidence(source="verified-aws-connection:conn-1", verified_at="last Tuesday"),
        )
        with pytest.raises(LeaseError) as exc:
            await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        assert exc.value.code == "invalid_evidence_time"

    async def test_same_action_reacquire_is_idempotent(self, session):
        """A retry after a crash must not self-conflict.

        The same action asking again refreshes its contact window rather than being
        refused, so recovery is not punished.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        second = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        assert second.applied
        assert second.lease.id == first.lease.id
        assert second.lease.revision == first.lease.revision + 1
        assert second.lease.lease_expires_at >= first.lease.lease_expires_at

    async def test_unrelated_targets_do_not_contend(self, session):
        """Two namespaces in one account are independent surfaces.

        If the identity were account-level these would block each other, and a lease
        that blocks unrelated work trains operators to bypass it.
        """
        a = await acquire_lease(session, target=_target(resource_id="cluster-a/ns-1"), holder=_holder(), manifest_entry_id=ENTRY)
        b = await acquire_lease(
            session,
            target=_target(resource_id="cluster-a/ns-2"),
            holder=_holder(action="action-2"),
            manifest_entry_id=ENTRY,
        )
        assert a.applied and b.applied
        assert a.lease.canonical_target_key != b.lease.canonical_target_key

    async def test_only_one_row_exists_per_target(self, session):
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        rows = (
            (
                await session.execute(
                    select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


class TestCrossTenantCollision:
    """THE story: aliases for one physical target collide, across tenants."""

    async def test_second_tenants_alias_for_one_cluster_is_refused(self, session):
        """Two connections, two tenants, one real cluster.

        If serialization were on the connection id both holders would believe they
        had exclusive access and would deploy incompatible releases on top of each
        other. The identity under lock is the physical surface, so the second alias
        contends with the first.
        """
        alias_a = _target(source="verified-aws-connection:conn-tenant-a")
        alias_b = _target(source="verified-aws-connection:conn-tenant-b")
        assert alias_a.canonical_key == alias_b.canonical_key

        first = await acquire_lease(session, target=alias_a, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        second = await acquire_lease(session, target=alias_b, holder=_holder(ORG_B, "action-b"), manifest_entry_id="other-entry")

        assert first.applied
        assert second.kind is LeaseOutcomeKind.CONFLICT
        assert second.reason == "target_held"

    async def test_refusal_discloses_nothing_about_the_holder(self, session):
        """A refusal that named the holder is a cross-tenant probe oracle.

        It would let any tenant discover the existence, ownership and deployment
        activity of another tenant's infrastructure by asking for a busy target.
        """
        await acquire_lease(session, target=_target(), holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        refused = await acquire_lease(session, target=_target(), holder=_holder(ORG_B, "action-b"), manifest_entry_id="other")

        assert refused.lease is None
        rendered = f"{refused.reason}"
        for leaked in (ORG_A, "action-a", ENTRY, "000000000000", "cluster-a", "conn-1"):
            assert leaked not in rendered

    async def test_same_tenant_other_action_gets_the_identical_refusal(self, session):
        """A distinguishable answer for "same tenant" would still be an oracle.

        Same-tenant and cross-tenant contention return byte-identical refusals, so a
        caller cannot use the difference to infer which tenant holds a target.
        """
        await acquire_lease(session, target=_target(), holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        same_tenant = await acquire_lease(session, target=_target(), holder=_holder(ORG_A, "action-z"), manifest_entry_id=ENTRY)
        other_tenant = await acquire_lease(session, target=_target(), holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)

        assert same_tenant.kind is other_tenant.kind is LeaseOutcomeKind.CONFLICT
        assert same_tenant.reason == other_tenant.reason
        assert same_tenant.lease is other_tenant.lease is None


class TestExpiryIsNotAnExit:
    """The guard most likely to be "simplified" into a timeout."""

    async def test_lapsed_but_unreconciled_lease_blocks_takeover(self, session):
        """THE rule.

        A deployment pipeline partitioned from us is still rolling pods. Expiry
        records when contact was expected and did not arrive — nothing more. Handing
        the target over on expiry alone is precisely the double-deploy this table
        exists to prevent.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        await _expire(session, target.canonical_key)

        refused = await acquire_lease(session, target=target, holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.reason == "target_held"
        assert refused.lease is None

        # And it stays blocked: this is fail-closed on purpose, not a bug to time out.
        still_refused = await acquire_lease(session, target=target, holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)
        assert still_refused.kind is LeaseOutcomeKind.CONFLICT

    async def test_reconciled_terminal_evidence_unblocks_takeover(self, session):
        """Only a process that actually looked can release the block.

        Reconciliation is the recorded observation that the previous deployment
        ended, and it is the sole route past a lapsed hold.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        await _expire(session, target.canonical_key, reconciled=EVIDENCE_TEXT)

        taken = await acquire_lease(session, target=target, holder=_holder(ORG_B, "action-b", generation=2), manifest_entry_id=ENTRY)
        assert taken.applied
        assert taken.lease.owner_org_id == ORG_B
        assert taken.lease.owner_action_id == "action-b"
        assert taken.lease.owner_generation == 2

    async def test_takeover_clears_the_previous_actions_evidence(self, session):
        """Stale evidence must not license the *next* expiry-based takeover.

        The evidence describes the previous action's ending. Leaving it in place
        would mean a later lapse is cleared by a reading of a deployment two holders
        ago — the takeover gate satisfied by an observation of the wrong thing.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        await _expire(session, target.canonical_key, reconciled=EVIDENCE_TEXT)
        taken = await acquire_lease(session, target=target, holder=_holder(ORG_B, "action-b", generation=2), manifest_entry_id=ENTRY)
        assert taken.lease.reconciled_terminal_evidence is None

        await _expire(session, target.canonical_key)
        refused = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-c", generation=3), manifest_entry_id=ENTRY)
        assert refused.kind is LeaseOutcomeKind.CONFLICT

    async def test_unexpired_lease_with_evidence_is_still_held(self, session):
        """Reconciliation alone is not a release.

        An observer that recorded a terminal reading has not thereby freed the
        target — the holder may still be finishing, and only `release_lease` (or a
        lapse plus that evidence) hands it on.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_A, "action-a"),
        )
        refused = await acquire_lease(session, target=target, holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)
        assert refused.kind is LeaseOutcomeKind.CONFLICT

    async def test_lapsed_at_does_not_mean_available(self, session):
        """The view's helper is named to resist the reading it would otherwise invite."""
        target = _target()
        outcome = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        assert not outcome.lease.lapsed_at(datetime.now(UTC))
        assert outcome.lease.lapsed_at(datetime.now(UTC) + timedelta(seconds=DEFAULT_LEASE_SECONDS + 1))


class TestReconcile:
    """Recording what was observed, and refreshing a holder's contact window."""

    async def test_reconcile_requires_terminal_evidence(self, session):
        """An unevidenced reconcile IS the expiry-based takeover this module refuses.

        If a blank string were accepted, any caller could clear the takeover gate
        without looking at anything.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        with pytest.raises(LeaseError) as exc:
            await reconcile_lease(
                session,
                canonical_target_key=target.canonical_key,
                expected_revision=first.lease.revision,
                terminal_evidence="   ",
                holder=_holder(),
            )
        assert exc.value.code == "missing_terminal_evidence"

    async def test_reconcile_records_evidence_and_advances_revision(self, session):
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        outcome = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=f"  {EVIDENCE_TEXT}  ",
            holder=_holder(),
        )
        assert outcome.applied
        assert outcome.lease.reconciled_terminal_evidence == EVIDENCE_TEXT
        assert outcome.lease.reconciled_at is not None
        assert outcome.lease.revision == first.lease.revision + 1
        # Still held: reconciling establishes what happened, releasing hands it on.
        assert outcome.lease.state is LeaseState.HELD

    async def test_stale_revision_is_retryable_and_writes_nothing(self, session):
        """A lost compare-and-set is STALE, not CONFLICT.

        Whatever moved the row may have changed what the caller should record, so the
        caller must re-read and decide again — and nothing is written in the meantime.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(),
        )
        stale = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence="a different reading",
            holder=_holder(),
        )
        assert stale.kind is LeaseOutcomeKind.STALE
        assert stale.reason == "stale_revision"
        row = await _row(session, target.canonical_key)
        assert row.reconciled_terminal_evidence == EVIDENCE_TEXT

    async def test_unknown_target_is_an_error_not_a_free_target(self, session):
        """Fail closed: "no such lease" must never read as "the target is free"."""
        with pytest.raises(LeaseError) as exc:
            await reconcile_lease(
                session,
                canonical_target_key="v1:" + "0" * 64,
                expected_revision=1,
                terminal_evidence=EVIDENCE_TEXT,
                holder=_holder(),
            )
        assert exc.value.code == "unknown_lease"

    @pytest.mark.parametrize("revision", [0, -1, True])
    async def test_invalid_expected_revision_is_refused(self, session, revision):
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        with pytest.raises(LeaseError):
            await reconcile_lease(
                session,
                canonical_target_key=target.canonical_key,
                expected_revision=revision,
                terminal_evidence=EVIDENCE_TEXT,
                holder=_holder(),
            )

    async def test_heartbeat_extends_the_holders_window(self, session):
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY, lease_seconds=60)
        outcome = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence="",
            heartbeat=True,
            holder=_holder(),
            lease_seconds=600,
        )
        assert outcome.applied
        assert outcome.lease.lease_expires_at > first.lease.lease_expires_at
        # A heartbeat records continuing liveness, not an ending.
        assert outcome.lease.reconciled_terminal_evidence is None

    async def test_both_modes_require_a_holder(self, session):
        """`holder` is mandatory for a reconcile too, not only for a heartbeat.

        It was briefly optional for the reconcile mode, on the reasoning that an
        observer reports what it saw and needs no authority to have looked. That is
        wrong: terminal evidence on this row is the ONLY thing that unblocks takeover
        of a lapsed target, so writing it is the takeover authorization. Making it
        optional let any caller that could derive the key manufacture its own licence
        to deploy over another tenant's cluster.

        Asserted as a TypeError because the guard is the signature itself — a keyword
        with no default cannot be omitted, which is a stronger guarantee than a
        runtime check a later refactor could reorder past.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)

        with pytest.raises(TypeError):
            await reconcile_lease(
                session,
                canonical_target_key=target.canonical_key,
                expected_revision=first.lease.revision,
                terminal_evidence=EVIDENCE_TEXT,
            )
        with pytest.raises(TypeError):
            await reconcile_lease(
                session,
                canonical_target_key=target.canonical_key,
                expected_revision=first.lease.revision,
                terminal_evidence="",
                heartbeat=True,
            )

    async def test_non_holder_cannot_keep_someone_elses_lease_alive(self, session):
        """Otherwise an unrelated actor could hold a target open indefinitely.

        The refusal is the opaque one: a distinguishable answer would let a caller
        probe which targets other tenants currently hold.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        refused = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence="",
            heartbeat=True,
            holder=_holder(ORG_B, "action-b"),
        )
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.reason == "target_held"
        assert refused.lease is None

    async def test_non_holder_stale_heartbeat_still_leaks_nothing(self, session):
        """A non-holder must not learn the row moved, either.

        Answering STALE with the lease attached would disclose the holder to a caller
        that guessed a revision — a refusal ordering bug that reads as harmless.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        refused = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=99,
            terminal_evidence="",
            heartbeat=True,
            holder=_holder(ORG_B, "action-b"),
        )
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.lease is None

    async def test_superseded_generation_cannot_heartbeat(self, session):
        """A superseded process keeping its old lease alive would starve its successor."""
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=1), manifest_entry_id=ENTRY)
        current = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=5), manifest_entry_id=ENTRY)
        refused = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=current.lease.revision,
            terminal_evidence="",
            heartbeat=True,
            holder=_holder(ORG_A, "action-a", generation=1),
        )
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.reason == "owner_generation_superseded"


class TestRelease:
    """Handing a target back, and who is permitted to."""

    async def test_holder_releases_and_the_target_becomes_acquirable(self, session):
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        released = await release_lease(
            session,
            canonical_target_key=target.canonical_key,
            holder=_holder(ORG_A, "action-a"),
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )
        assert released.applied
        # `state` is the single authority on whether the target is held; the owner
        # columns survive as the last-holder record, which is what makes a retried
        # terminal callback recognisable as the same holder.
        assert released.lease.state is LeaseState.FREE
        assert released.lease.manifest_entry_id is None
        assert released.lease.lease_expires_at is None

        taken = await acquire_lease(session, target=target, holder=_holder(ORG_B, "action-b"), manifest_entry_id="other")
        assert taken.applied

    async def test_release_preserves_the_canonicalization_evidence(self, session):
        """The row outlives the hold on purpose.

        An operator must still be able to ask why two aliases were treated as one
        target after the deployment finished.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        released = await release_lease(
            session,
            canonical_target_key=target.canonical_key,
            holder=_holder(),
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )
        assert released.lease.evidence_source == "verified-aws-connection:conn-1"
        assert released.lease.canonical_target_key == target.canonical_key

    async def test_release_requires_terminal_evidence(self, session):
        """An unevidenced release is a lease lapse by another name.

        And this is the call that makes the target re-acquirable, so it must record
        why it was safe to do so.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        with pytest.raises(LeaseError) as exc:
            await release_lease(
                session,
                canonical_target_key=target.canonical_key,
                holder=_holder(),
                reason=ReleaseReason.COMPLETED,
                terminal_evidence="",
            )
        assert exc.value.code == "missing_terminal_evidence"

    async def test_non_holder_cannot_release(self, session):
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        refused = await release_lease(
            session,
            canonical_target_key=target.canonical_key,
            holder=_holder(ORG_B, "action-b"),
            reason=ReleaseReason.ABANDONED,
            terminal_evidence=EVIDENCE_TEXT,
        )
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.reason == "target_held"
        assert refused.lease is None
        # And the holder still holds it.
        row = await _row(session, target.canonical_key)
        assert row.state == LeaseState.HELD.value
        assert row.owner_action_id == "action-a"

    async def test_repeated_release_is_idempotent(self, session):
        """A retried terminal callback is not a failure.

        At-least-once delivery on the completion path must not produce a spurious
        error that an operator then investigates.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        for _ in range(2):
            outcome = await release_lease(
                session,
                canonical_target_key=target.canonical_key,
                holder=_holder(),
                reason=ReleaseReason.COMPLETED,
                terminal_evidence=EVIDENCE_TEXT,
            )
            assert outcome.applied

    async def test_unknown_target_is_an_error(self, session):
        with pytest.raises(LeaseError) as exc:
            await release_lease(
                session,
                canonical_target_key="v1:" + "0" * 64,
                holder=_holder(),
                reason=ReleaseReason.COMPLETED,
                terminal_evidence=EVIDENCE_TEXT,
            )
        assert exc.value.code == "unknown_lease"


class TestStaleGenerationCannotRelease:
    """THE stale-actor guard."""

    async def test_superseded_actor_cannot_free_its_successors_hold(self, session):
        """The worst available outcome if this were applied.

        A superseded actor's release would free the target *while its successor is
        actively deploying to it*, handing the cluster to whoever asks next. That is
        worse than the deployment it was trying to tidy up after, which is why this
        is a terminal refusal and not a tolerated no-op.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=1), manifest_entry_id=ENTRY)
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=7), manifest_entry_id=ENTRY)

        refused = await release_lease(
            session,
            canonical_target_key=target.canonical_key,
            holder=_holder(ORG_A, "action-a", generation=1),
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.reason == "owner_generation_superseded"
        row = await _row(session, target.canonical_key)
        assert row.state == LeaseState.HELD.value
        assert row.owner_generation == 7

    async def test_generation_never_goes_backwards(self, session):
        """Lowering it would hand the fence back to a superseded actor.

        Which would make the actor look current again on its next write — the whole
        failure mode the generation exists to close.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=9), manifest_entry_id=ENTRY)
        outcome = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=9), manifest_entry_id=ENTRY)
        assert outcome.lease.owner_generation == 9
        refused = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=2), manifest_entry_id=ENTRY)
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.reason == "owner_generation_superseded"

    async def test_superseded_actor_cannot_reacquire_after_release(self, session):
        """A freed row keeps its generation, so the same actor cannot slip back in.

        This is why `release_lease` deliberately retains `owner_generation` rather
        than clearing it with the other holder columns.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=4), manifest_entry_id=ENTRY)
        await release_lease(
            session,
            canonical_target_key=target.canonical_key,
            holder=_holder(ORG_A, "action-a", generation=4),
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )
        row = await _row(session, target.canonical_key)
        assert row.owner_generation == 4

    async def test_new_action_is_not_fenced_by_the_previous_actions_generation(self, session):
        """The fence is per action, not per row.

        A new holder arriving at generation 1 must not inherit a *stranger's*
        high-water mark of 7. Clamping to the row's maximum across a change of action
        would leave the new legitimate holder permanently superseded: refused on its
        own first heartbeat, and unable to release the lease it actually holds — a
        wedged target that no operator could free through the normal path.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=7), manifest_entry_id=ENTRY)
        await release_lease(
            session,
            canonical_target_key=target.canonical_key,
            holder=_holder(ORG_A, "action-a", generation=7),
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )

        successor = _holder(ORG_B, "action-b", generation=1)
        taken = await acquire_lease(session, target=target, holder=successor, manifest_entry_id="other-entry")
        assert taken.applied
        assert taken.lease.owner_generation == 1

        # And the new holder can actually exercise its authority.
        beat = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=taken.lease.revision,
            terminal_evidence="",
            heartbeat=True,
            holder=successor,
        )
        assert beat.applied
        freed = await release_lease(
            session,
            canonical_target_key=target.canonical_key,
            holder=successor,
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )
        assert freed.applied


class TestReconcileAuthority:
    """Terminal evidence may only be written by a caller with standing over the holder.

    This class exists because of a fail-open defect: `reconcile_lease` took an
    optional `holder` and, in the non-heartbeat mode, checked nothing at all. The
    reasoning that produced it sounds right — an observer reports what it saw, and
    looking requires no authority. But the evidence it writes is the *only* thing
    that unblocks takeover of a lapsed target, so the write IS the takeover
    authorization, and it needs the takeover's authority.

    The attack it enabled, end to end: a tenant legitimately owns connection X, which
    aliases the same physical cluster another tenant is deploying to. It derives the
    canonical key from its own connection (no privileged information required),
    submits any string as `terminal_evidence`, waits for the real holder's contact
    window to lapse, and acquires a cluster that is still being deployed to.
    """

    async def test_foreign_tenant_cannot_write_terminal_evidence(self, session):
        """The whole defect, asserted directly.

        Org B has an alias for org A's physical target and tries to manufacture the
        evidence that would license its own takeover.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)

        refused = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence="fabricated: run 999 concluded success",
            holder=_holder(ORG_B, "action-b"),
        )

        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.reason == "target_held"
        assert refused.lease is None
        # Nothing was written, so the takeover gate is still closed.
        row = await _row(session, target.canonical_key)
        assert row.reconciled_terminal_evidence is None
        assert row.reconciled_at is None

    async def test_refused_evidence_does_not_enable_takeover(self, session):
        """The consequence, not just the refusal.

        Proves the two halves connect: after the unauthorized reconcile is refused,
        the lapsed lease still blocks takeover. A version that refused the call but
        wrote the row anyway would pass the test above and fail this one.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence="fabricated",
            holder=_holder(ORG_B, "action-b"),
        )
        await _expire(session, target.canonical_key)

        stolen = await acquire_lease(session, target=target, holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)
        assert stolen.kind is LeaseOutcomeKind.CONFLICT
        assert stolen.lease is None

    async def test_same_org_different_action_has_no_standing(self, session):
        """Standing is the action, not the tenant.

        A sibling action in the same org is still not the holder. Checking only
        `org_id` would let any action in a tenant reconcile any other's deployment —
        a weaker fence that looks correct in a single-tenant test.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        refused = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_A, "action-other"),
        )
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.lease is None

    async def test_superseded_generation_cannot_reconcile(self, session):
        """A displaced process of the right action cannot write evidence either.

        The lease IS returned here, unlike the foreign-caller case: this caller holds
        the correct action and only its own binding lapsed, so it needs to see what
        superseded it. Same asymmetry `release_lease` applies.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=1), manifest_entry_id=ENTRY)
        current = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a", generation=5), manifest_entry_id=ENTRY)

        refused = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=current.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_A, "action-a", generation=1),
        )
        assert refused.kind is LeaseOutcomeKind.CONFLICT
        assert refused.reason == "owner_generation_superseded"
        row = await _row(session, target.canonical_key)
        assert row.reconciled_terminal_evidence is None

    async def test_holder_can_still_reconcile(self, session):
        """The carve-out is not so tight that the legitimate path broke.

        Without this, every test above would also pass if `reconcile_lease` refused
        unconditionally.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        applied = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_A, "action-a"),
        )
        assert applied.applied
        assert applied.lease.reconciled_terminal_evidence == EVIDENCE_TEXT


class TestReconcileDisclosure:
    """A caller without standing cannot distinguish contention from staleness.

    The defect this guards: the revision compare-and-set ran BEFORE the standing
    check, and a mismatch returned `_to_view(row)`. So a tenant holding its own alias
    for a shared physical target could present a deliberately wrong revision and read
    the real holder's org, action, manifest entry, release ref and evidence out of the
    `STALE` answer. Ordering, not a missing check — which is why it reads as harmless.
    """

    async def test_stale_revision_from_a_foreign_caller_leaks_nothing(self, session):
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)

        refused = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=9999,  # deliberately wrong
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_B, "action-b"),
        )

        assert refused.kind is LeaseOutcomeKind.CONFLICT, "a foreign caller must not be told its revision moved"
        assert refused.reason == "target_held"
        assert refused.lease is None

    async def test_foreign_caller_gets_an_identical_answer_either_way(self, session):
        """Indistinguishability is the actual property, so compare the two answers.

        If a correct revision and a wrong one produce different answers, the
        difference is itself an oracle: a caller can search for the real revision and
        learn when it changes, i.e. when the other tenant is deploying.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)

        with_correct = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_B, "action-b"),
        )
        with_wrong = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=4242,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_B, "action-b"),
        )

        assert (with_correct.kind, with_correct.reason, with_correct.lease) == (with_wrong.kind, with_wrong.reason, with_wrong.lease)
        assert with_correct.lease is None


class TestEvidenceRefreshOnTake:
    """The row's canonicalization evidence describes the hold it actually authorized.

    There is ONE durable row per physical target for the platform's life, so a row
    acquired today may have been inserted months ago by a different tenant through a
    different alias. The defect: `_take` wrote the holder columns but left
    `evidence_source` / `evidence_verified_at` / `evidence_detail` at their
    insert-time values, so a new holder inherited the previous tenant's evidence and
    the row no longer recorded what proved THIS acquisition. `evidence_source` is the
    field an operator consults to answer "what proved these two aliases are the same
    place?" — a stale answer there is worse than none, being indistinguishable from a
    fresh one.
    """

    async def test_cross_tenant_reacquire_replaces_the_evidence(self, session):
        target_a = _target(source="verified-aws-connection:conn-A")
        await acquire_lease(session, target=target_a, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        await release_lease(
            session,
            canonical_target_key=target_a.canonical_key,
            holder=_holder(ORG_A, "action-a"),
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )

        # Org B's alias for the SAME physical target — same canonical key, different
        # readback. The key must match or this tests nothing.
        target_b = _target(source="verified-aws-connection:conn-B")
        assert target_b.canonical_key == target_a.canonical_key

        taken = await acquire_lease(session, target=target_b, holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)
        assert taken.applied
        assert taken.lease.evidence_source == "verified-aws-connection:conn-B"
        row = await _row(session, target_a.canonical_key)
        assert row.evidence_source == "verified-aws-connection:conn-B"

    async def test_reconciled_takeover_replaces_the_evidence(self, session):
        """The other route onto an existing row: lapsed plus reconciled evidence."""
        target_a = _target(source="verified-aws-connection:conn-A")
        first = await acquire_lease(session, target=target_a, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        await reconcile_lease(
            session,
            canonical_target_key=target_a.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_A, "action-a"),
        )
        await _expire(session, target_a.canonical_key, reconciled=EVIDENCE_TEXT)

        target_b = _target(source="verified-aws-connection:conn-B")
        taken = await acquire_lease(session, target=target_b, holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)
        assert taken.applied
        assert taken.lease.evidence_source == "verified-aws-connection:conn-B"

    async def test_evidence_timestamp_and_detail_are_refreshed_too(self, session):
        """All three evidence columns, not only the one a spot-check would notice."""
        target_a = _target(source="verified-aws-connection:conn-A")
        await acquire_lease(session, target=target_a, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)
        await release_lease(
            session,
            canonical_target_key=target_a.canonical_key,
            holder=_holder(ORG_A, "action-a"),
            reason=ReleaseReason.COMPLETED,
            terminal_evidence=EVIDENCE_TEXT,
        )

        fresh = PhysicalTarget(
            provider="aws",
            account_id="000000000000",
            region="us-east-1",
            resource_kind="eks-namespace",
            resource_id="cluster-a/adp-gateway",
            evidence=TargetEvidence(
                source="verified-aws-connection:conn-B",
                verified_at="2026-09-19T12:00:00+00:00",
                detail="sts:AssumeRole readback for conn-B",
            ),
        )
        assert fresh.canonical_key == target_a.canonical_key

        await acquire_lease(session, target=fresh, holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)
        row = await _row(session, target_a.canonical_key)
        assert row.evidence_detail == "sts:AssumeRole readback for conn-B"
        assert row.evidence_verified_at.replace(tzinfo=UTC) == datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class TestTerminalVersusRetryable:
    """The two refusal classes stay distinct."""

    async def test_conflict_and_stale_are_different_kinds(self, session):
        """Collapsing them is how a displaced actor keeps acting.

        A caller told "retry" when it has actually lost authority will retry its way
        back into deploying to a target somebody else holds.
        """
        target = _target()
        first = await acquire_lease(session, target=target, holder=_holder(ORG_A, "action-a"), manifest_entry_id=ENTRY)

        conflict = await acquire_lease(session, target=target, holder=_holder(ORG_B, "action-b"), manifest_entry_id=ENTRY)
        # The holder reconciles (it has standing), moving the revision...
        await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_A, "action-a"),
        )
        # ...and then presents its now-stale revision again. STALE, not CONFLICT:
        # it still holds authority, it just lost a compare-and-set.
        stale = await reconcile_lease(
            session,
            canonical_target_key=target.canonical_key,
            expected_revision=first.lease.revision,
            terminal_evidence=EVIDENCE_TEXT,
            holder=_holder(ORG_A, "action-a"),
        )

        assert conflict.kind is LeaseOutcomeKind.CONFLICT
        assert stale.kind is LeaseOutcomeKind.STALE
        assert conflict.kind is not stale.kind

    async def test_outcome_kinds_are_the_complete_vocabulary(self):
        """No catch-all member, so an unhandled case cannot be silently bucketed."""
        assert {k.value for k in LeaseOutcomeKind} == {"applied", "stale", "conflict"}

    async def test_unknown_stored_state_is_refused_not_defaulted(self, session):
        """A newer writer's state must not be read as "free".

        Defaulting an unrecognised state to free would hand out a target a newer pod
        deliberately holds — a fail-open on exactly the rolling-deploy window where
        two builds coexist.
        """
        target = _target()
        await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        row = await _row(session, target.canonical_key)
        row.state = "draining"
        await session.flush()
        with pytest.raises(LeaseError) as exc:
            await acquire_lease(session, target=target, holder=_holder(), manifest_entry_id=ENTRY)
        assert exc.value.code == "unknown_vocabulary"


class TestTransactionBoundary:
    """The caller owns the transaction, and no network call happens inside it."""

    async def test_store_commits_nothing(self, session_factory):
        """A store that committed would publish a hold the caller then rolled back.

        The lease would outlive the decision that took it, leaving a target held by
        an action that never started.
        """
        target = _target()
        async with session_factory() as writing:
            await acquire_lease(writing, target=target, holder=_holder(), manifest_entry_id=ENTRY)
            await writing.rollback()
        async with session_factory() as reading:
            rows = (
                (
                    await reading.execute(
                        select(OrchestrationEnvironmentLease).where(OrchestrationEnvironmentLease.canonical_target_key == target.canonical_key)
                    )
                )
                .scalars()
                .all()
            )
            assert rows == []

    async def test_acquire_takes_a_target_not_a_key_string(self, session):
        """The signature is the guard.

        A `PhysicalTarget` cannot be constructed without readback evidence, so there
        is no code path in which a lease is taken for an identity derived from a
        caller-supplied account string.
        """
        with pytest.raises((ManifestError, AttributeError, TypeError)):
            await acquire_lease(session, target="v1:" + "0" * 64, holder=_holder(), manifest_entry_id=ENTRY)
