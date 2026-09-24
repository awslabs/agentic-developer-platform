"""The domain-owned budget ledger, against real PostgreSQL. Issue #5535 (W6).

Set ``SUPERPLANE_TEST_POSTGRES_URL`` to a ``postgresql+asyncpg`` URL. Each test
creates and removes its own random schema; no provider, cloud or B service is
contacted, and a green run here is NOT live acceptance evidence.

## Why this suite cannot be SQLite, and why that is not a preference

Every property the ledger claims is enforced by the database rather than by Python,
which is the design — so a test that did not use the real database would be
asserting almost nothing the design relies on:

* ``UNIQUE (job_id, attempt_id)`` closes the window a ``SELECT``-then-``INSERT`` in
  application code leaves open between its two statements. The case it exists for
  is two replicas admitting the same attempt simultaneously, so it is only
  observable with real connections racing.
* ``SELECT ... FOR UPDATE`` is what stops two concurrent confirms both observing
  ``reserved``. SQLite has no row locks, so that assertion would pass vacuously.
* The blank-guard ``CHECK`` constraints use the POSIX operator ``!~``, which
  SQLite's parser rejects outright — which is why
  `app/models/operation_budget.py` carries the ``postgresql_bootstrap_journal``
  marker keeping this table out of the HTTP double's ``create_all``.

## The properties, in the order they matter

1. **A repeat under the same attempt key returns the same reservation** — what
   makes a lost reply recoverable rather than a second spend.
2. **A repeat with a *changed* envelope is DENIED** — honouring it is how a retry
   becomes a budget increase, applied after admission, with nothing recording that
   it happened.
3. **A reservation survives the process that made it** — the reservation is durable
   state, not process state, so a replica that dies between reserving and
   confirming must not free the budget it was holding.

## `BudgetDenied` versus `BudgetUnavailable` is asserted by TYPE, never by message

Inside the harness's `_confirm` an unavailable ledger **retains** the reservation
while a denial **releases** it (`harness_jobs/admission.py:874-900`), so the two are
opposite instructions about budget. A test that accepted "some exception" would pass
against an adapter that freed budget for work that may still be running.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from asyncpg.exceptions import CheckViolationError, UniqueViolationError
from harness_jobs.admission import BudgetDenied, BudgetUnavailable, Reservation
from harness_jobs.approval import SpendEnvelope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.adapters.harness_connection import HarnessConnections
from app.adapters.operation_budget_ledger import (
    OperationBudgetLedger,
    WorkspaceBudgetLimits,
)
from app.models.operation_budget import (
    STATE_CONFIRMED,
    STATE_RELEASED,
    STATE_RESERVED,
    STATE_RETAINED,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("SUPERPLANE_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL database",
)

TABLE = "operation_budget_reservations"


def envelope(units: int = 4, seconds: int = 3600, micros: int = 5_000_000):
    """A real `SpendEnvelope`.

    The actual harness type rather than a local stand-in, throughout. A stand-in
    would keep passing after the real dataclass gained a field the ledger must
    store, and this suite's whole purpose is that the two sides of the seam agree.
    It is cheap here because the envelope is frozen and takes three integers.
    """
    return SpendEnvelope(
        max_resource_units=units,
        max_runtime_seconds=seconds,
        max_cost_micros=micros,
    )


def keys() -> dict[str, str]:
    """A fresh attempt identity. Random per call so no two tests can collide."""
    return {
        "job_id": "job-" + uuid.uuid4().hex,
        "attempt_id": "attempt-" + uuid.uuid4().hex,
        "org_id": "org-" + uuid.uuid4().hex,
        "workspace_id": "ws-" + uuid.uuid4().hex,
    }


def _apply_migration(schema: str):
    """Create the table by running migration 018, not ``Base.metadata``.

    The migration rather than the model because the migration is what a deployment
    actually receives. A table built from the model would assert that the
    *declaration* is right while saying nothing about the shipped DDL, and the
    constraints are this suite's subject. `tests/test_models.py` pins the
    declaration; `test_the_shipped_ddl_matches_the_declared_model` below closes the
    loop between them.
    """
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/018_add_operation_budget_reservations.py"
    )
    spec = importlib.util.spec_from_file_location("budget_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection) -> None:
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    return run


@pytest.fixture
async def ledger():
    """A ledger over its own schema, plus the means to restart its connections.

    Yields ``(ledger, connections, restart)``. ``restart`` closes the current pool
    and returns a ledger on a brand-new one against the same schema — which is how
    the durability tests model a replica dying, rather than by opening a second
    connection on a live pool. A second connection would not establish that the
    reservation outlived the process's own state.

    ``ssl="disable"`` because this is a local throwaway database with no certificate
    to check; `app/schema_boundary.py` is deliberately fail-closed on transport, and
    this states the exception locally rather than inheriting the process-wide
    environment variable `conftest` sets for the SQLite double.
    """
    url = os.environ["SUPERPLANE_TEST_POSTGRES_URL"]
    schema = "budget_test_" + uuid.uuid4().hex
    dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.run_sync(_apply_migration(schema))

    pools: list[HarnessConnections] = []

    async def build() -> HarnessConnections:
        connections = HarnessConnections(
            dsn,
            {
                "server_settings": {
                    "search_path": schema,
                    "statement_timeout": "10000",
                },
                "ssl": "disable",
            },
        )
        await connections.open()
        pools.append(connections)
        return connections

    try:
        connections = await build()

        async def restart():
            await connections.aclose()
            fresh = await build()
            return OperationBudgetLedger(fresh.connect), fresh

        yield OperationBudgetLedger(connections.connect), connections, restart
    finally:
        for pool in pools:
            await pool.aclose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def row(connections, job_id: str, attempt_id: str):
    async with connections.connect() as connection:
        return await connection.fetchrow(
            f"SELECT * FROM {TABLE} WHERE job_id=$1 AND attempt_id=$2",
            job_id,
            attempt_id,
        )


async def count(connections, job_id: str) -> int:
    async with connections.connect() as connection:
        return await connection.fetchval(
            f"SELECT count(*) FROM {TABLE} WHERE job_id=$1", job_id
        )


class TestIdempotentRepeat:
    """Property 1: a retry under the same attempt key is not a second spend."""

    async def test_reserving_twice_returns_the_same_reservation_and_one_row(
        self, ledger
    ):
        """The lost-reply case: the caller never saw the first answer.

        Both halves are asserted, because either alone is satisfied by a wrong
        implementation. Matching ids alone would pass against an adapter that
        inserted a second row with a derived id — a double hold with a consistent
        identifier. One row alone would pass against an adapter that raised.
        """
        ledger, connections, _ = ledger
        identity = keys()

        first = await ledger.reserve(envelope=envelope(), **identity)
        second = await ledger.reserve(envelope=envelope(), **identity)

        assert isinstance(first, Reservation)
        assert second.reservation_id == first.reservation_id
        assert await count(connections, identity["job_id"]) == 1
        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_RESERVED

    async def test_the_reservation_id_is_derived_not_random(self, ledger):
        """A second ledger computes the same id without reading the first's row.

        Why it matters: a retry that cannot see the prior row still computes the same
        identifier, so a lost reply cannot leave two differently-identified rows for
        one attempt even if the unique constraint were ever dropped. The constraint
        is the enforcement; this is the belt.
        """
        ledger, _connections, restart = ledger
        identity = keys()
        first = await ledger.reserve(envelope=envelope(), **identity)

        after, fresh = await restart()
        again = await after.reserve(envelope=envelope(), **identity)

        assert again.reservation_id == first.reservation_id
        assert await count(fresh, identity["job_id"]) == 1

    async def test_confirming_the_same_approved_envelope_twice_is_a_no_op(self, ledger):
        """Idempotent confirm, because the harness retries this path.

        An exception on the second call would turn a duplicate compensation into a
        failure whose only recovery is to retry again.
        """
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)

        approved = envelope(units=2, seconds=60, micros=1_000_000)
        await ledger.confirm(reservation=reservation, envelope=approved)
        await ledger.confirm(reservation=reservation, envelope=approved)

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_CONFIRMED
        # The APPROVED values, not the requested ones. An approval may legitimately
        # be for less than was asked, and the ledger must record what was granted —
        # `SpendEnvelope.covers()` is one-directional for the same reason.
        assert stored["max_resource_units"] == 2
        assert stored["max_runtime_seconds"] == 60
        assert stored["max_cost_micros"] == 1_000_000

    async def test_releasing_twice_is_a_no_op(self, ledger):
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)

        await ledger.release(reservation=reservation, reason="provider reported absent")
        await ledger.release(reservation=reservation, reason="provider reported absent")

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_RELEASED

    async def test_retaining_twice_is_a_no_op(self, ledger):
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)

        await ledger.retain(reservation=reservation, reason="provider unreachable")
        await ledger.retain(reservation=reservation, reason="provider unreachable")

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_RETAINED

    async def test_a_confirmed_reservation_may_still_be_released(self, ledger):
        """Confirm binds the approved envelope; absence can be established after.

        Asserted because the state machine is not a straight line, and an
        implementation that treated `confirmed` as terminal would hold budget for
        work a provider later reported it never started.
        """
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)
        await ledger.confirm(reservation=reservation, envelope=envelope())

        await ledger.release(reservation=reservation, reason="provider holds nothing")

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_RELEASED


class TestAChangedEnvelopeIsDenied:
    """Property 2: the rule whose absence turns a retry into a budget increase."""

    @pytest.mark.parametrize(
        ("units", "seconds", "micros"),
        [
            (8, 3600, 5_000_000),
            (4, 7200, 5_000_000),
            (4, 3600, 10_000_000),
            # Smaller, too. A retry asking for LESS still changed the envelope it was
            # admitted under, and the recorded envelope would no longer describe what
            # the approval covered. The ledger does not get to decide that a smaller
            # ask is harmless: reconciliation is against the envelope the attempt was
            # admitted with, not the smallest one ever requested.
            (1, 60, 1),
        ],
        ids=["more-units", "more-runtime", "more-cost", "less-of-everything"],
    )
    async def test_a_repeat_with_a_different_envelope_is_denied(
        self, ledger, units, seconds, micros
    ):
        ledger, connections, _ = ledger
        identity = keys()
        await ledger.reserve(envelope=envelope(), **identity)

        with pytest.raises(BudgetDenied):
            await ledger.reserve(
                envelope=envelope(units=units, seconds=seconds, micros=micros),
                **identity,
            )

        # Still one row, still the ORIGINAL envelope. A denial that had already
        # written would be worse than honouring the change, because the caller would
        # be told no while the new limit quietly took effect.
        assert await count(connections, identity["job_id"]) == 1
        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["max_resource_units"] == 4
        assert stored["max_runtime_seconds"] == 3600
        assert stored["max_cost_micros"] == 5_000_000

    async def test_the_denial_is_a_permissionerror_and_not_a_runtimeerror(self, ledger):
        """Asserted on the hierarchy, because the harness branches on exactly this.

        `BudgetDenied` is a `PermissionError`; `BudgetUnavailable` is a
        `RuntimeError`. If a changed envelope raised the latter, `_confirm` would take
        the retain branch — holding budget forever for a request that will never be
        permitted, instead of releasing it.
        """
        ledger, _, _ = ledger
        identity = keys()
        await ledger.reserve(envelope=envelope(), **identity)

        with pytest.raises(BudgetDenied) as caught:
            await ledger.reserve(envelope=envelope(units=9), **identity)
        assert isinstance(caught.value, PermissionError)
        assert not isinstance(caught.value, BudgetUnavailable)

    @pytest.mark.parametrize("field", ["org_id", "workspace_id"])
    async def test_the_same_attempt_key_for_another_tenant_is_denied(
        self, ledger, field
    ):
        """Either a key collision or an attempt to spend another tenant's budget.

        Neither is resolved by taking the new value, so both are denied.
        """
        ledger, _, _ = ledger
        identity = keys()
        await ledger.reserve(envelope=envelope(), **identity)

        other = dict(identity, **{field: "other-" + uuid.uuid4().hex})
        with pytest.raises(BudgetDenied):
            await ledger.reserve(envelope=envelope(), **other)

    async def test_the_denial_does_not_disclose_the_holding_tenant(self, ledger):
        """The caller learns its identity does not match, not whose does.

        Echoing the stored tenant would make this a tenant-enumeration oracle:
        submit a guessed attempt key and read back who holds it.
        """
        ledger, _, _ = ledger
        identity = keys()
        await ledger.reserve(envelope=envelope(), **identity)

        other = dict(identity, workspace_id="ws-" + uuid.uuid4().hex)
        with pytest.raises(BudgetDenied) as caught:
            await ledger.reserve(envelope=envelope(), **other)
        assert identity["workspace_id"] not in str(caught.value)
        assert identity["org_id"] not in str(caught.value)

    async def test_confirming_a_different_approved_envelope_is_denied(self, ledger):
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)
        await ledger.confirm(reservation=reservation, envelope=envelope(units=2))

        with pytest.raises(BudgetDenied):
            await ledger.confirm(reservation=reservation, envelope=envelope(units=3))

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["max_resource_units"] == 2

    async def test_re_reserving_a_released_attempt_is_denied(self, ledger):
        """Released means established absence — the budget was genuinely returned.

        Re-reserving under the same key would spend it a second time. A new attempt
        needs a new attempt id, which is what makes the second spend visible.
        """
        ledger, _, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)
        await ledger.release(reservation=reservation, reason="absent")

        with pytest.raises(BudgetDenied):
            await ledger.reserve(envelope=envelope(), **identity)

    async def test_a_retained_reservation_may_not_be_released(self, ledger):
        """Retained means "we do not know". Freeing it is the double-spend.

        The single most load-bearing assertion in this file: `retained` exists
        precisely so an unreconciled reservation is not treated as free, and an
        adapter that allowed this transition would make the state a comment.
        """
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)
        await ledger.retain(reservation=reservation, reason="provider unreachable")

        with pytest.raises(BudgetDenied):
            await ledger.release(reservation=reservation, reason="assuming absent")

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_RETAINED

    async def test_a_released_reservation_may_not_be_retained(self, ledger):
        """The other direction: absence was established, so it is not an unknown."""
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)
        await ledger.release(reservation=reservation, reason="absent")

        with pytest.raises(BudgetDenied):
            await ledger.retain(reservation=reservation, reason="unsure after all")

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_RELEASED

    @pytest.mark.parametrize("settle", ["release", "retain"])
    async def test_a_settled_reservation_may_not_be_confirmed(self, ledger, settle):
        ledger, _, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)
        await getattr(ledger, settle)(reservation=reservation, reason="settled")

        with pytest.raises(BudgetDenied):
            await ledger.confirm(reservation=reservation, envelope=envelope())

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    async def test_settling_requires_a_reason(self, ledger, blank):
        """Denied in the adapter rather than surfaced as a constraint violation.

        The check constraint would refuse it anyway; refusing here names the caller's
        mistake instead of leaking a driver message that quotes the offending row —
        and the row carries tenant identifiers. Whitespace-only is included because
        `str.strip()` and a bare `btrim` disagree about tabs.
        """
        ledger, _, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)

        with pytest.raises(BudgetDenied):
            await ledger.release(reservation=reservation, reason=blank)
        with pytest.raises(BudgetDenied):
            await ledger.retain(reservation=reservation, reason=blank)

    async def test_confirming_something_never_reserved_is_denied(self, ledger):
        """A denial, not an unavailable: the store answered, and it holds nothing."""
        ledger, _, _ = ledger
        identity = keys()
        phantom = Reservation(
            reservation_id="deadbeef",
            job_id=identity["job_id"],
            attempt_id=identity["attempt_id"],
        )

        with pytest.raises(BudgetDenied):
            await ledger.confirm(reservation=phantom, envelope=envelope())

    async def test_settling_something_never_reserved_is_denied(self, ledger):
        ledger, _, _ = ledger
        identity = keys()
        phantom = Reservation(
            reservation_id="deadbeef",
            job_id=identity["job_id"],
            attempt_id=identity["attempt_id"],
        )

        with pytest.raises(BudgetDenied):
            await ledger.release(reservation=phantom, reason="nothing here")


class TestConcurrency:
    """The cases that exist only against a real database."""

    async def test_concurrent_reserves_of_one_attempt_produce_one_row(self, ledger):
        """Two replicas admitting the same attempt at the same moment.

        This is the window a `SELECT`-then-`INSERT` in application code leaves open.
        Eight concurrent callers: every one must come back with the same reservation
        id, and the table must hold one row. Passing here is the unique constraint
        doing its job — `_insert`'s `ON CONFLICT` turns the loser's failure into a
        read of the winner's row.
        """
        ledger, connections, _ = ledger
        identity = keys()

        results = await asyncio.gather(
            *(ledger.reserve(envelope=envelope(), **identity) for _ in range(8)),
            return_exceptions=True,
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        assert failures == [], failures
        assert len({r.reservation_id for r in results}) == 1
        assert await count(connections, identity["job_id"]) == 1

    async def test_concurrent_reserves_with_different_envelopes_admit_exactly_one(
        self, ledger
    ):
        """The adversarial version: the race IS the changed-envelope attack.

        Six callers, six different envelopes, submitted together. Exactly one may be
        admitted and the rest must be DENIED — not merely "one row exists", because
        an implementation where the last writer won would also leave one row. So the
        admitted count and the denial count are both asserted exactly, and anything
        that is neither is surfaced rather than tolerated.
        """
        ledger, connections, _ = ledger
        identity = keys()
        envelopes = [envelope(units=n) for n in range(1, 7)]

        results = await asyncio.gather(
            *(ledger.reserve(envelope=e, **identity) for e in envelopes),
            return_exceptions=True,
        )

        admitted = [r for r in results if isinstance(r, Reservation)]
        refused = [r for r in results if isinstance(r, BudgetDenied)]
        unexpected = [
            r for r in results if not isinstance(r, (Reservation, BudgetDenied))
        ]
        assert unexpected == [], unexpected
        assert len(admitted) == 1
        assert len(refused) == len(envelopes) - 1
        assert await count(connections, identity["job_id"]) == 1

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["max_resource_units"] in range(1, 7)

    async def test_concurrent_confirms_do_not_overwrite_each_other(self, ledger):
        """`FOR UPDATE` is what stops two confirms both observing `reserved`.

        Without the row lock both would read `reserved`, both would proceed, and the
        second would overwrite the first's approved envelope — so the recorded budget
        would be whichever confirm happened to land second, which is not a decision
        anyone made. Either ordering is fine; what must not happen is both
        succeeding.

        ## Why the interleaving is forced rather than merely awaited

        The obvious version of this test — ``asyncio.gather`` over two confirms —
        passes even with `FOR UPDATE` deleted, which I verified by deleting it. Two
        reasons, and both make the naive test worthless: the pool hands the calls
        connections in sequence, and each confirm is short enough that the first
        transaction commits before the second reads. So the two never overlap, the
        second sees `confirmed`, and the "one success, one denial" assertion is
        satisfied by ordinary sequencing rather than by any lock.

        `_fetch_locked` is therefore wrapped to make both readers hold their
        transactions open until both have read. That is the state the lock exists to
        prevent, and with the lock present the second reader blocks inside its own
        ``SELECT`` instead of reaching the barrier — so the barrier is released by a
        timeout rather than by the second arrival, and the test still completes. The
        wrapper is on the instance and the fixture's ledger is per-test, so nothing
        leaks.
        """
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)

        # Both readers wait here. `wait_for` with a short timeout rather than a bare
        # `wait`, because when the lock IS held the second reader never arrives and a
        # bare wait would hang the suite instead of passing.
        both_have_read = asyncio.Event()
        arrived = 0
        original = ledger._fetch_locked

        async def barrier(connection, job_id, attempt_id):
            nonlocal arrived
            existing = await original(connection, job_id, attempt_id)
            arrived += 1
            if arrived >= 2:
                both_have_read.set()
            else:
                with __import__("contextlib").suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(both_have_read.wait(), timeout=1.0)
            return existing

        ledger._fetch_locked = barrier

        results = await asyncio.gather(
            ledger.confirm(reservation=reservation, envelope=envelope(units=2)),
            ledger.confirm(reservation=reservation, envelope=envelope(units=5)),
            return_exceptions=True,
        )

        succeeded = [r for r in results if r is None]
        denials = [r for r in results if isinstance(r, BudgetDenied)]
        assert len(succeeded) == 1, results
        assert len(denials) == 1, results

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_CONFIRMED
        assert stored["max_resource_units"] in (2, 5)

    async def test_a_concurrent_release_and_retain_reach_one_terminal_state(
        self, ledger
    ):
        """The compensation race. One wins; the loser must not silently overwrite.

        Both are compensations the harness may run concurrently on recovery, and they
        mean opposite things about whether budget is free. A last-writer outcome
        would let a retain be erased by a release that established nothing.
        """
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)

        results = await asyncio.gather(
            ledger.release(reservation=reservation, reason="absent"),
            ledger.retain(reservation=reservation, reason="unreachable"),
            return_exceptions=True,
        )

        unexpected = [
            r for r in results if r is not None and not isinstance(r, BudgetDenied)
        ]
        assert unexpected == [], unexpected
        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["state"] in (STATE_RELEASED, STATE_RETAINED)
        assert (stored["reason"] or "").strip() != ""


class TestDurability:
    """Property 3: the reservation is durable state, not process state."""

    async def test_a_reservation_survives_the_process_that_made_it(self, ledger):
        """The crash case: a replica dies between reserving and confirming.

        Modelled by closing the pool entirely and building a new one — what a restart
        does — rather than by opening a second connection on a live pool, which would
        not establish that the reservation outlived the process's state.

        If budget were process state, the restarted ledger would happily reserve
        again and the workspace would pay twice for one attempt. Asserted in both
        directions: the repeat returns the same reservation, AND the changed-envelope
        denial still applies across the restart.
        """
        ledger, _, restart = ledger
        identity = keys()
        first = await ledger.reserve(envelope=envelope(), **identity)

        after, fresh = await restart()

        again = await after.reserve(envelope=envelope(), **identity)
        assert again.reservation_id == first.reservation_id

        with pytest.raises(BudgetDenied):
            await after.reserve(envelope=envelope(units=9), **identity)

        assert await count(fresh, identity["job_id"]) == 1

    async def test_a_reserved_row_is_not_freed_by_a_restart(self, ledger):
        """A restart must not implicitly release. State stays exactly as committed.

        The tempting shortcut — sweep `reserved` rows on startup, since "nobody is
        holding them" — is the failure this pins. A reserved row belongs to an attempt
        that may still be running, and age cannot distinguish a dead attempt from a
        slow one. Only the harness's recovery path or an operator settles it.
        """
        ledger, _, restart = ledger
        identity = keys()
        await ledger.reserve(envelope=envelope(), **identity)

        _after, fresh = await restart()

        stored = await row(fresh, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_RESERVED

    async def test_a_confirmed_envelope_survives_a_restart(self, ledger):
        ledger, _, restart = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)
        await ledger.confirm(reservation=reservation, envelope=envelope(units=2))

        after, fresh = await restart()

        stored = await row(fresh, identity["job_id"], identity["attempt_id"])
        assert stored["state"] == STATE_CONFIRMED
        assert stored["max_resource_units"] == 2
        # And it is still idempotent after the restart.
        await after.confirm(reservation=reservation, envelope=envelope(units=2))


class TestTheTransportIsClassifiedAsUnavailable:
    """The availability/denial split, asserted at the transport rather than argued.

    This class caught a real defect while being written: `_acquire` wrapped only the
    synchronous ``connect()`` call, so a closed pool — which raises on
    ``__aenter__`` — escaped as `HarnessDatabaseUnavailable`, matching neither of
    `_confirm`'s except clauses. `_session` now covers entry and commit.
    """

    async def test_a_closed_pool_reports_unavailable_and_never_denied(self, ledger):
        """A ledger whose pool is gone has not answered the question.

        Reporting it as `BudgetDenied` would make `_confirm` RELEASE budget for work
        that may be running. Reporting it as anything else — including the connection
        layer's own exception — matches no branch at all, so the compensation is
        skipped and the reservation is stranded in `reserved` with nothing recording
        why. A database restart is enough to reach both.
        """
        ledger, connections, _ = ledger
        await connections.aclose()

        with pytest.raises(BudgetUnavailable) as caught:
            await ledger.reserve(envelope=envelope(), **keys())
        assert not isinstance(caught.value, BudgetDenied)
        assert isinstance(caught.value, RuntimeError)

    @pytest.mark.parametrize("method", ["confirm", "release", "retain"])
    async def test_every_method_classifies_a_closed_pool_the_same_way(
        self, ledger, method
    ):
        """All four, not just `reserve`. The compensations are where it matters most.

        `release` and `retain` run on the recovery path, which is exactly when a
        database has just been unreachable — so an unclassified error there is more
        likely than in the request path, not less.
        """
        ledger, connections, _ = ledger
        identity = keys()
        reservation = await ledger.reserve(envelope=envelope(), **identity)
        await connections.aclose()

        arguments = (
            {"envelope": envelope()} if method == "confirm" else {"reason": "gone"}
        )
        with pytest.raises(BudgetUnavailable):
            await getattr(ledger, method)(reservation=reservation, **arguments)

    async def test_no_dsn_fragment_reaches_the_unavailable_message(self, ledger):
        """The message reaches a capability readout, so it must carry no target.

        An asyncpg connection error quotes the DSN, and a DSN carries a password. So
        the adapter's messages are its own prose; this asserts the driver's are not
        passed through.
        """
        ledger, connections, _ = ledger
        url = os.environ["SUPERPLANE_TEST_POSTGRES_URL"]
        await connections.aclose()

        with pytest.raises(BudgetUnavailable) as caught:
            await ledger.reserve(envelope=envelope(), **keys())

        message = str(caught.value)
        assert "password" not in message.lower()
        fragments = url.replace("//", " ").replace("@", " ").replace("/", " ").split()
        for fragment in fragments:
            if len(fragment) > 8:
                assert fragment not in message

    @pytest.mark.parametrize(
        "bad",
        [
            {
                "max_resource_units": True,
                "max_runtime_seconds": 1,
                "max_cost_micros": 1,
            },
            {"max_resource_units": 1, "max_runtime_seconds": 1.5, "max_cost_micros": 1},
            {"max_resource_units": 1, "max_runtime_seconds": 1, "max_cost_micros": -1},
            {"max_resource_units": 1, "max_runtime_seconds": 1},
        ],
        ids=["bool", "float", "negative", "missing-field"],
    )
    async def test_a_malformed_envelope_is_unavailable_not_denied(self, ledger, bad):
        """A contract breach by the caller is not a tenant's denial.

        Unavailable is the conservative direction here precisely because it retains
        rather than releases: the ledger genuinely did not answer the budget
        question, so it must not cause budget to be freed. `bool` is in the list
        because `True` is an `int` in Python and would otherwise store as a
        silently-tiny envelope of 1.

        A plain object rather than a `SpendEnvelope`, because the real dataclass
        refuses all of these in `__post_init__` — which is the point: the envelope
        reaches the ledger through a Protocol, so it is whatever the caller passed.
        """
        ledger, _, _ = ledger
        malformed = type("Malformed", (), bad)()

        with pytest.raises(BudgetUnavailable):
            await ledger.reserve(envelope=malformed, **keys())


class TestWorkspaceLimits:
    """Caps come from the workspace's own declared limits, or there is no cap."""

    @staticmethod
    def capped(connections, **limits):
        async def limits_for(*, org_id, workspace_id):
            return WorkspaceBudgetLimits(**limits)

        return OperationBudgetLedger(connections.connect, limits_for=limits_for)

    async def test_a_reservation_over_the_cost_cap_is_denied(self, ledger):
        _ledger, connections, _ = ledger
        identity = keys()
        capped = self.capped(connections, max_cost_micros=1_000_000)

        with pytest.raises(BudgetDenied):
            await capped.reserve(envelope=envelope(micros=2_000_000), **identity)

        assert await count(connections, identity["job_id"]) == 0

    async def test_a_reservation_over_the_resource_cap_is_denied(self, ledger):
        _ledger, connections, _ = ledger
        capped = self.capped(connections, max_resource_units=2)

        with pytest.raises(BudgetDenied):
            await capped.reserve(envelope=envelope(units=3), **keys())

    async def test_the_cap_message_does_not_disclose_the_configured_ceiling(
        self, ledger
    ):
        """A caller does not need the workspace's ceiling to know it was exceeded."""
        _ledger, connections, _ = ledger
        capped = self.capped(connections, max_cost_micros=1_234_567)

        with pytest.raises(BudgetDenied) as caught:
            await capped.reserve(envelope=envelope(micros=2_000_000), **keys())
        assert "1234567" not in str(caught.value).replace(",", "")

    async def test_retained_budget_still_counts_but_released_budget_does_not(
        self, ledger
    ):
        """The distinction `COMMITTED_STATES` exists to make, exercised end to end.

        `released` means established absence, so the budget is genuinely free again.
        `retained` means "held because we do not know" — and treating an unreconciled
        reservation as free is how the same budget gets spent twice. An
        implementation that simply excluded every settled state would pass the
        released half and fail the retained half, which is why both are here against
        one cap.
        """
        _ledger, connections, _ = ledger
        capped = self.capped(connections, max_cost_micros=1_000_000)
        org, workspace = "org-" + uuid.uuid4().hex, "ws-" + uuid.uuid4().hex

        def attempt() -> dict[str, str]:
            return {
                "job_id": "job-" + uuid.uuid4().hex,
                "attempt_id": "attempt-" + uuid.uuid4().hex,
                "org_id": org,
                "workspace_id": workspace,
            }

        held = attempt()
        reservation = await capped.reserve(envelope=envelope(micros=1_000_000), **held)

        # The cap is now fully committed, so nothing more fits.
        with pytest.raises(BudgetDenied):
            await capped.reserve(envelope=envelope(micros=1), **attempt())

        # Retained is still committed: an unknown does not free budget.
        await capped.retain(reservation=reservation, reason="provider unreachable")
        with pytest.raises(BudgetDenied):
            await capped.reserve(envelope=envelope(micros=1), **attempt())

        # Now release the retained row's sibling instead: a second attempt admitted
        # under a wider ledger, then released. Released budget must not count.
        other = attempt()
        wider = self.capped(connections, max_cost_micros=10_000_000)
        freed = await wider.reserve(envelope=envelope(micros=500_000), **other)
        await capped.release(reservation=freed, reason="provider holds nothing")

        # Still denied, because the retained million is what fills the cap — not the
        # released half-million. This assertion fails if `released` were counted; the
        # earlier one fails if `retained` were not.
        with pytest.raises(BudgetDenied):
            await capped.reserve(envelope=envelope(micros=1), **attempt())

    async def test_the_cap_is_scoped_to_one_workspace(self, ledger):
        """A neighbour's spend must not deny this workspace.

        The limit query filters on `workspace_id`; without it, one busy workspace
        would exhaust every other workspace's budget in the same table.
        """
        _ledger, connections, _ = ledger
        capped = self.capped(connections, max_cost_micros=1_000_000)

        await capped.reserve(envelope=envelope(micros=1_000_000), **keys())
        # A wholly different workspace, at the same cap, is unaffected.
        assert await capped.reserve(envelope=envelope(micros=1_000_000), **keys())

    async def test_the_caps_are_re_read_per_reservation(self, ledger):
        """Lowering a workspace's budget takes effect on the next attempt.

        `limits_for` is injected and awaited per reserve rather than captured at
        composition, so a budget change does not wait for a restart. Asserted by
        changing the answer between two calls.
        """
        _ledger, connections, _ = ledger
        ceiling = {"value": 10_000_000}

        async def limits_for(*, org_id, workspace_id):
            return WorkspaceBudgetLimits(max_cost_micros=ceiling["value"])

        live = OperationBudgetLedger(connections.connect, limits_for=limits_for)
        assert await live.reserve(envelope=envelope(micros=2_000_000), **keys())

        ceiling["value"] = 1_000_000
        with pytest.raises(BudgetDenied):
            await live.reserve(envelope=envelope(micros=2_000_000), **keys())

    async def test_an_unconfigured_workspace_is_not_denied(self, ledger):
        """No declared limit is not a limit of zero, and it is not a bypass.

        The caps consulted are the workspace's OWN declared limits. Inventing one
        where none is declared would deny every operation in every workspace that has
        not set a budget, which reads as the product being broken rather than as a
        missing setting.
        """
        _ledger, connections, _ = ledger

        async def no_limits(*, org_id, workspace_id):
            return None

        unconstrained = OperationBudgetLedger(connections.connect, limits_for=no_limits)
        assert await unconstrained.reserve(envelope=envelope(micros=10**12), **keys())

    async def test_a_declared_zero_denies(self, ledger):
        """The other half of the same distinction: a deliberate zero is honoured."""
        _ledger, connections, _ = ledger
        capped = self.capped(connections, max_cost_micros=0)

        with pytest.raises(BudgetDenied):
            await capped.reserve(envelope=envelope(micros=1), **keys())

    async def test_an_unset_dimension_does_not_cap_that_dimension(self, ledger):
        """A cost cap says nothing about resource units, and must not imply one."""
        _ledger, connections, _ = ledger
        capped = self.capped(connections, max_cost_micros=10_000_000)

        assert await capped.reserve(envelope=envelope(units=10**6, micros=1), **keys())


class TestTheConstraintsAreReal:
    """The database guarantees the adapter relies on instead of re-checking on read."""

    @staticmethod
    async def insert(connections, **overrides):
        values = {
            "reservation_id": "r-" + uuid.uuid4().hex,
            "job_id": "j-" + uuid.uuid4().hex,
            "attempt_id": "a-" + uuid.uuid4().hex,
            "org_id": "o-" + uuid.uuid4().hex,
            "workspace_id": "w-" + uuid.uuid4().hex,
            "state": STATE_RESERVED,
            "max_resource_units": 1,
            "max_runtime_seconds": 1,
            "max_cost_micros": 1,
            "reason": None,
        }
        values.update(overrides)
        async with connections.connect() as connection:
            await connection.execute(
                f"INSERT INTO {TABLE} (reservation_id, job_id, attempt_id, org_id, "
                "workspace_id, state, max_resource_units, max_runtime_seconds, "
                "max_cost_micros, reason) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                *values.values(),
            )

    @pytest.mark.parametrize(
        "column",
        ["reservation_id", "job_id", "attempt_id", "org_id", "workspace_id"],
    )
    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    async def test_a_blank_identifier_is_rejected_by_the_database(
        self, ledger, column, blank
    ):
        """The POSIX `!~` guards, exercised. These are what SQLite cannot parse.

        Asserted against the database rather than against the adapter, because the
        adapter's correctness argument is "the column cannot hold a blank" — not "the
        adapter checks". A blank tenant id that reached storage would be a
        reservation nothing could attribute, and no limit applies to a reservation
        nobody can attribute. The tab case is why the constraint uses `[[:space:]]`
        rather than `btrim`.
        """
        _ledger, connections, _ = ledger

        with pytest.raises(CheckViolationError) as caught:
            await self.insert(connections, **{column: blank})
        assert getattr(caught.value, "sqlstate", None) == "23514"

    @pytest.mark.parametrize(
        "column", ["max_resource_units", "max_runtime_seconds", "max_cost_micros"]
    )
    async def test_a_negative_envelope_is_rejected_by_the_database(
        self, ledger, column
    ):
        """A negative `max_cost_micros` in a SUM() is a budget increase in disguise."""
        _ledger, connections, _ = ledger

        with pytest.raises(CheckViolationError) as caught:
            await self.insert(connections, **{column: -1})
        assert getattr(caught.value, "sqlstate", None) == "23514"

    async def test_an_unknown_state_is_rejected_by_the_database(self, ledger):
        """The four-state vocabulary is enforced, not merely documented."""
        _ledger, connections, _ = ledger

        with pytest.raises(CheckViolationError) as caught:
            await self.insert(connections, state="probably-fine", reason="x")
        assert getattr(caught.value, "sqlstate", None) == "23514"

    @pytest.mark.parametrize("state", [STATE_RELEASED, STATE_RETAINED])
    async def test_a_compensated_row_must_carry_a_reason(self, ledger, state):
        """A row settled by a compensation without one is unexplainable.

        `released` and `retained` are the two states an operator finds later and
        needs explained, and they are exactly the two the Protocol gives a `reason`.
        """
        _ledger, connections, _ = ledger

        with pytest.raises(CheckViolationError) as caught:
            await self.insert(connections, state=state, reason=None)
        assert getattr(caught.value, "sqlstate", None) == "23514"

        with pytest.raises(CheckViolationError):
            await self.insert(connections, state=state, reason="  ")

    @pytest.mark.parametrize("state", [STATE_RESERVED, STATE_CONFIRMED])
    async def test_a_reserved_or_confirmed_row_needs_no_reason(self, ledger, state):
        """Both exemptions, pinned — and `confirmed` is the load-bearing one.

        Stated as an implication rather than a plain NOT NULL because neither state
        has a reason to give: `reserved` has not been settled, and `confirmed` is the
        reservation being honoured rather than abandoned. `confirm` receives only an
        envelope from the Protocol, so an adapter that had to supply a reason here
        could only invent one.

        This is a regression test with a real history. An earlier revision of
        migration 018 exempted only `reserved`, which made every `confirm` violate
        the check — and because `_translate` maps an unrecognized write failure to
        `BudgetUnavailable`, the harness's `_confirm` would have RETAINED every
        admitted operation's budget forever while reporting a database fault. The
        five confirm tests above are what found it; this one states the rule directly
        so it cannot come back through a hand-edited constraint.
        """
        _ledger, connections, _ = ledger
        await self.insert(connections, state=state, reason=None)

    async def test_the_attempt_key_is_unique(self, ledger):
        """Asserted directly on the constraint, not only through the adapter.

        The adapter's `ON CONFLICT` behaviour depends on this existing. A test that
        only went through `reserve` would still pass if the constraint were dropped
        and the adapter's re-read happened to win the race.
        """
        _ledger, connections, _ = ledger
        shared = {"job_id": "j-" + uuid.uuid4().hex, "attempt_id": "a-1"}

        await self.insert(connections, **shared)
        with pytest.raises(UniqueViolationError) as caught:
            await self.insert(connections, **shared)
        assert getattr(caught.value, "sqlstate", None) == "23505"

    async def test_the_cost_column_holds_more_than_thirty_two_bits(self, ledger):
        """`BigInteger`, because money is integer micros.

        A workspace budget in the tens of dollars already exceeds 32 bits, so an
        `Integer` column would overflow on an ordinary configuration rather than on an
        extreme one.
        """
        ledger, connections, _ = ledger
        identity = keys()
        big = 9 * 10**15

        await ledger.reserve(envelope=envelope(micros=big), **identity)

        stored = await row(connections, identity["job_id"], identity["attempt_id"])
        assert stored["max_cost_micros"] == big

    async def test_the_shipped_ddl_matches_the_declared_model(self, ledger):
        """Migration 018 and `app/models/operation_budget.py` must not drift.

        The migration is what a deployment receives; the model is what Alembic
        autogeneration compares against. If they disagree, the next autogenerated
        revision "fixes" the difference in whichever direction the model happens to
        say — so the disagreement has to fail here first. This suite builds its table
        from the migration, so comparing against the model closes the loop.
        """
        from app.models.operation_budget import OperationBudgetReservation

        _ledger, connections, _ = ledger
        async with connections.connect() as connection:
            observed = {
                record["column_name"]
                for record in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $1",
                    TABLE,
                )
            }

        declared = {
            column.name for column in OperationBudgetReservation.__table__.columns
        }
        assert observed == declared
