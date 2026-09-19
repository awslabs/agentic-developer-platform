"""Adopting a legacy lane is explicit, refusable and disabled by default (#5144).

`test_handoff.py` covers the cheap refusals — the flag, the policy binding, the
reconciliation attestations — because those are decided before any claim is read. This
file drives adoption through the *real* `work_claims.force_handover` with the real
liveness harness, because the guard that actually matters cannot be tested with a stub:

> the prior owner's lease and liveness must be reconciled, and `unverifiable` must
> refuse exactly as `live` does.

`unverifiable` is the load-bearing case. It is what a partitioned-but-working worker
looks like, and what a long-running worker with a stalled status writer looks like.
Treating either as gone is how the old and the adopting owner end up committing to the
same branch — the double-effect this story exists to prevent, reintroduced by its own
recovery path.

Adoption delegates to `force_handover` rather than reimplementing the transfer, so
these tests also serve as the assertion that the delegation is real: a second transfer
path with its own weaker guards would pass a test written against a stub and fail here.

The tests are written against the durable claim state, not only the returned exception,
because "it raised" is compatible with a half-completed transfer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.handoff import ADOPTION_ENABLED_ENV, AdoptionRefusedError, adopt_legacy_lane
from src.orchestration.models import ClaimState, OrchestrationWorkClaim
from src.orchestration.work_claims import (
    ClaimBinding,
    ClaimOwner,
    Disposition,
    OwnerKind,
    ReleaseReason,
    bind_run,
    claim_work,
)
from src.shared.models.base import Base

ORG_A = "org-alpha"
REPO_ID = 987_654_321
ISSUE = 5144
PLAN_VERSION = 3
RUN = "legacy-run-1"


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
async def session(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s


@pytest.fixture(autouse=True)
def _adoption_on(monkeypatch):
    """Default to enabled, so a refusal below is a real guard and not the flag.

    The flag's own default is asserted separately in `test_handoff.py`; here it must be
    out of the way or every test would pass for the wrong reason.
    """
    monkeypatch.setenv(ADOPTION_ENABLED_ENV, "true")


class FakeLivenessResolver:
    """Stands in for the DynamoDB-backed `RunBindingResolver`.

    Same shape as `test_work_claims.py`'s, deliberately: the liveness verdicts this
    drives are the prior owner's, and two divergent fakes would let the two suites
    disagree about what `unverifiable` means.
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


def _live_row() -> dict:
    return {
        "event_id": RUN,
        "status": "in_progress",
        "arrived_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status_updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _exited_row() -> dict:
    return {
        "event_id": RUN,
        "status": "complete",
        "arrived_at": "2026-09-01T00:00:00Z",
        "status_updated_at": "2026-09-01T01:00:00Z",
    }


def _unverifiable_row() -> dict:
    """Active status whose last signal is far outside the staleness window."""
    return {
        "event_id": RUN,
        "status": "in_progress",
        "arrived_at": "2026-01-01T00:00:00Z",
        "status_updated_at": "2026-01-01T00:00:00Z",
    }


async def _legacy_lane(session, *, run_id: str = RUN):
    """A held claim bound to a prior owner's run — the lane to be adopted."""
    receipt = await claim_work(
        session,
        binding=ClaimBinding(org_id=ORG_A, provider_repository_id=REPO_ID, issue_number=ISSUE),
        owner=ClaimOwner(OwnerKind.DIRECT_DISPATCH, "resident-coordinator"),
        event_id="legacy-event",
    )
    await bind_run(session, org_id=ORG_A, claim_id=receipt.claim_id, generation=receipt.generation, run_id=run_id)
    return receipt


async def _adopt(session, claim_id: str, resolver, **overrides):
    kwargs = {
        "org_id": ORG_A,
        "claim_id": claim_id,
        "decision_id": "decision-1",
        "resolver": resolver,
        "effects_reconciled": True,
        "credentials_reconciled": True,
        "accepted_plan_version": PLAN_VERSION,
    }
    kwargs.update(overrides)
    return await adopt_legacy_lane(session, **kwargs)


# ---------------------------------------------------------------------------
# The prior owner's liveness is the guard that cannot be attested away
# ---------------------------------------------------------------------------


async def test_a_live_prior_owner_refuses_adoption(session):
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({RUN: _live_row()})

    with pytest.raises(AdoptionRefusedError) as caught:
        await _adopt(session, receipt.claim_id, resolver)

    assert caught.value.code == "run_live"
    # The durable state: the lane still belongs to its original owner.
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.HELD.value
    assert claim.generation == receipt.generation
    assert claim.active_run_id == RUN


async def test_an_unverifiable_prior_owner_refuses_adoption(session):
    """The important one: loss of contact is not evidence of an exit.

    A partitioned worker and an exited worker are indistinguishable from the platform's
    side, so adopting on `unverifiable` would put two owners on one lane precisely when
    the platform has least visibility into it.
    """
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({RUN: _unverifiable_row()})

    with pytest.raises(AdoptionRefusedError) as caught:
        await _adopt(session, receipt.claim_id, resolver)

    assert caught.value.code == "run_unverifiable"
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.HELD.value
    assert claim.active_run_id == RUN


async def test_a_faulted_liveness_lookup_refuses_rather_than_degrading(session):
    """Unavailable authority fails closed. No fallback to a weaker signal."""
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver(fault=True)

    with pytest.raises(AdoptionRefusedError) as caught:
        await _adopt(session, receipt.claim_id, resolver)

    assert caught.value.code == "liveness_unavailable"
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.HELD.value


async def test_an_absent_liveness_record_refuses(session):
    """Absence of a record is not evidence of absence; the row may be unwritten yet."""
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({})

    with pytest.raises(AdoptionRefusedError) as caught:
        await _adopt(session, receipt.claim_id, resolver)

    assert caught.value.code == "liveness_unknown"
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.HELD.value


async def test_an_expired_lease_alone_does_not_permit_adoption(session):
    """The tempting shortcut the design forbids.

    A lease is a liveness *hint*, not a verdict — a worker whose heartbeat stalled still
    holds its branch. Adoption needs the liveness verdict regardless of the lease.
    """
    receipt = await _legacy_lane(session)
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    claim.lease_expires_at = datetime.now(UTC) - timedelta(hours=5)
    await session.flush()
    resolver = FakeLivenessResolver({RUN: _live_row()})

    with pytest.raises(AdoptionRefusedError):
        await _adopt(session, receipt.claim_id, resolver)

    refreshed = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert refreshed.state == ClaimState.HELD.value


# ---------------------------------------------------------------------------
# Reconciliation is required even with a proven exit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attestation", ["effects_reconciled", "credentials_reconciled"])
async def test_unreconciled_state_refuses_a_cleanly_exited_lane(session, attestation):
    """A proven exit is necessary, not sufficient.

    An already-issued GitHub installation token survives any database fence, so an
    unattested credential state is a real hazard rather than paperwork.
    """
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({RUN: _exited_row()})

    with pytest.raises(AdoptionRefusedError) as caught:
        await _adopt(session, receipt.claim_id, resolver, **{attestation: False})

    assert caught.value.code == "reconciliation_outstanding"
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.HELD.value
    assert claim.generation == receipt.generation


# ---------------------------------------------------------------------------
# The positive case, so the refusals above are a gate and not a permanent block
# ---------------------------------------------------------------------------


async def test_a_reconciled_exited_lane_is_adopted_exactly_once(session):
    """Release-then-reclaim: the lane is left RELEASED at the next generation.

    Not spliced to a new owner in place — that shape could not guarantee the resident
    coordinator and the engine never hold one lane simultaneously. The adopter goes
    through ordinary admission afterwards.
    """
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({RUN: _exited_row()})

    result = await _adopt(session, receipt.claim_id, resolver)

    assert result.disposition is Disposition.ADMITTED
    assert result.generation == receipt.generation + 1
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.RELEASED.value
    assert claim.release_reason == ReleaseReason.HANDOVER.value
    # No owner holds it at the moment of transfer, which is what makes two
    # simultaneous owners impossible.
    assert claim.active_run_id is None
    assert claim.generation == receipt.generation + 1


async def test_a_second_adoption_of_the_same_lane_does_not_advance_again(session):
    """Repeating the operator action must not ratchet the generation.

    Each advance invalidates the previous owner's fences, so an adoption that could be
    replayed would be a way to invalidate a healthy new owner.
    """
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({RUN: _exited_row()})
    first = await _adopt(session, receipt.claim_id, resolver)

    with pytest.raises(AdoptionRefusedError) as caught:
        await _adopt(session, receipt.claim_id, resolver)

    assert caught.value.code == "claim_not_held"
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.generation == first.generation


async def test_adoption_requires_a_recorded_authorizing_decision(session):
    """Handover is an operator act; a competing dispatch cannot grant it to itself."""
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({RUN: _exited_row()})

    with pytest.raises(AdoptionRefusedError) as caught:
        await _adopt(session, receipt.claim_id, resolver, decision_id="")

    assert caught.value.code == "missing_decision"
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.HELD.value


async def test_a_disabled_deployment_refuses_before_reading_any_claim(session):
    """The flag is checked first, so a disabled deployment does no claim work at all."""
    import os

    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({RUN: _exited_row()})
    os.environ[ADOPTION_ENABLED_ENV] = "false"
    try:
        with pytest.raises(AdoptionRefusedError) as caught:
            await _adopt(session, receipt.claim_id, resolver)
    finally:
        os.environ[ADOPTION_ENABLED_ENV] = "true"

    assert caught.value.code == "adoption_disabled"
    # Never consulted, so a disabled deployment cannot even probe liveness.
    assert resolver.calls == []
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.HELD.value


async def test_cross_tenant_claim_id_cannot_be_adopted(session):
    """A claim id from another tenant is refused as nonexistent, not as forbidden."""
    receipt = await _legacy_lane(session)
    resolver = FakeLivenessResolver({RUN: _exited_row()})

    with pytest.raises(AdoptionRefusedError) as caught:
        await _adopt(session, receipt.claim_id, resolver, org_id="org-other")

    assert caught.value.code == "unknown_claim"
    claim = await session.get(OrchestrationWorkClaim, receipt.claim_id)
    assert claim.state == ClaimState.HELD.value
