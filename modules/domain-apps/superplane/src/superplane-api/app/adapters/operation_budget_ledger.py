"""The domain's ``BudgetLedger``: budget authority the harness refuses to hold.

Issue #5535 (Superplane W6), EPIC #4910.

`harness_jobs` declares `BudgetLedger` as a Protocol and ships no implementation,
and `modules/harness/jobs/tests/test_admission_bypass.py:221` fails if it ever
does. That is not a gap waiting on another team — it is a statement about where
budget authority lives. Only the domain knows a workspace's limits and what it has
already committed, so only the domain can answer "may this attempt hold this
envelope". `OperationFacadeService.__post_init__` raises `ContractViolation` when
`ledger is None`, so the facade port cannot be composed at all until this exists.

## The one property that matters

Idempotent on ``(job_id, attempt_id)``, and a repeat under the same key with a
*changed envelope* must be **denied** rather than honoured
(`harness_jobs/admission.py:391-436`). The reason the second half is not a
nicety: honouring it is how a retry becomes a budget increase. A caller that
retries with a larger envelope and gets a fresh reservation has raised its own
limit, and nothing in the system records that it happened.

Both halves are enforced by the database, not by this code:

* `UNIQUE (job_id, attempt_id)` means a second INSERT under the same key fails.
  This matters because the concurrent case is two replicas admitting the same
  attempt simultaneously, where a `SELECT`-then-`INSERT` in application code has a
  window between the statements and a unique index does not. The adapter's
  `ON CONFLICT` clause turns that race into a read of the winning row.
* The stored envelope columns let the conflict path *compare* rather than assume.
  A digest would establish inequality without being able to say what changed.

## Why this adapter does not use SQLAlchemy

It writes over the harness's asyncpg connection, through the same `connect`
callable the facade uses. Three reasons, in order of how badly each would break:

1. **The ledger is called from inside admission.** `admit_operation` invokes
   `reserve` while holding its advisory lock, and `release`/`retain` as
   compensating actions afterwards. Those compensations must commit even when the
   admission they compensate for is rolling back — which is precisely what a
   ledger sharing the request's SQLAlchemy transaction could not do: the release
   would roll back with the failure it was compensating for, and the budget would
   stay held forever with nothing recording why.
2. **A request session is not always available.** Admission also runs from
   recovery paths that have no HTTP request and therefore no session dependency.
3. **The harness's connection is guaranteed idle and exclusively owned** for the
   length of its context, which is what `_admission_ownership` requires. Reaching
   for a second, session-bound connection inside that window would be a second
   lock scope nobody can see.

Each public method therefore opens its own connection and its own transaction.
That is deliberate: a compensation that commits independently of the thing it
compensates for is the whole point.

## Availability versus denial, and why they must never be confused

`BudgetUnavailable` is a `RuntimeError` — the question was not answered.
`BudgetDenied` is a `PermissionError` — the answer is no, durably, and a retry
gets the same answer. The consequence is inside the harness's `_confirm`: an
unavailable ledger during confirm **retains** the reservation, where a denial
**releases** it. So translating a database outage into `BudgetDenied` would
release budget for work that may be running, and translating a real denial into
`BudgetUnavailable` would invite an endless retry of something that will never be
permitted. Every `except` clause below picks one deliberately.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app.models.operation_budget import (
    COMMITTED_STATES,
    STATE_CONFIRMED,
    STATE_RELEASED,
    STATE_RESERVED,
    STATE_RETAINED,
)

logger = logging.getLogger(__name__)

_TABLE = "operation_budget_reservations"

# PostgreSQL's serialization-failure and deadlock SQLSTATEs. Both mean "retry may
# succeed", so they are reported as unavailable rather than denied: the question
# was not answered. Matched on the code rather than the message because messages
# are localized.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


@dataclass(frozen=True)
class WorkspaceBudgetLimits:
    """The caps a workspace's own configuration places on one attempt.

    Micros, to match `SpendEnvelope`. ``None`` on a field means the workspace
    declares no limit for that dimension, which is **not** the same as a limit of
    zero — a workspace with no configured daily budget is unconstrained on cost,
    whereas one configured at zero may not spend. Collapsing the two with a
    ``or 0`` default would silently deny every operation in an unconfigured
    workspace, and collapsing them the other way would ignore a deliberate zero.
    """

    max_cost_micros: int | None = None
    max_resource_units: int | None = None


class OperationBudgetLedger:
    """A durable, attempt-keyed budget ledger over the harness's connection.

    Constructed with the same ``connect`` callable the facade is composed with, so
    the ledger and the operation store are always the same database. A ledger
    pointed at a different database than the operations it reserves for would
    produce reservations nothing could reconcile.

    ``limits_for`` is injected rather than implemented here: resolving a
    workspace's configured caps is domain policy that reads the `workspaces` row,
    and this class's job is the durable attempt bookkeeping. Injecting it also
    means the caps can be re-read per reservation instead of captured once at
    composition, so lowering a workspace's budget takes effect on the next attempt
    rather than on the next restart.
    """

    def __init__(
        self,
        connect: Any,
        limits_for: Any = None,
    ) -> None:
        self._connect = connect
        self._limits_for = limits_for

    # ------------------------------------------------------------------
    # BudgetLedger
    # ------------------------------------------------------------------

    async def reserve(
        self,
        *,
        job_id: str,
        attempt_id: str,
        org_id: str,
        workspace_id: str,
        envelope: Any,
    ) -> Any:
        """Hold budget for one attempt. Idempotent on ``(job_id, attempt_id)``.

        The INSERT carries `ON CONFLICT (job_id, attempt_id) DO NOTHING` and then
        reads the row back, so the three outcomes are distinguished by what is in
        the database rather than by what this process believed beforehand:

        * no prior row -> inserted, this is the reservation;
        * prior row, same envelope -> the same `Reservation` is returned and no
          second effect occurs, which is what makes a retry safe;
        * prior row, different envelope -> `BudgetDenied`.

        The tenant is compared too. A repeat under the same attempt key naming a
        *different* workspace is denied rather than honoured: the key is supposed
        to identify one attempt at one piece of work, and the same key arriving for
        another tenant means either a collision or an attempt to spend another
        tenant's budget. Neither should be resolved by taking the new value.
        """
        from harness_jobs.admission import BudgetDenied, Reservation

        requested = _envelope_values(envelope)
        reservation_id = _reservation_id(job_id, attempt_id)

        async with self._session() as connection:
            existing = await self._fetch_locked(connection, job_id, attempt_id)

            if existing is None:
                await self._check_limits(
                    connection,
                    org_id=org_id,
                    workspace_id=workspace_id,
                    requested=requested,
                )
                inserted = await self._insert(
                    connection,
                    reservation_id=reservation_id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    org_id=org_id,
                    workspace_id=workspace_id,
                    requested=requested,
                )
                if inserted is None:
                    # Lost the insert race under the unique constraint. The
                    # winner's row is authoritative; re-read and fall through
                    # to the same comparison a sequential retry would make.
                    existing = await self._fetch_locked(connection, job_id, attempt_id)
                else:
                    return Reservation(
                        reservation_id=inserted["reservation_id"],
                        job_id=job_id,
                        attempt_id=attempt_id,
                    )

            if existing is None:  # pragma: no cover - defensive
                raise _unavailable("the budget reservation could not be established")

            if existing["org_id"] != org_id or existing["workspace_id"] != workspace_id:
                # Deliberately does not echo the stored tenant: the caller
                # supplied one identity and is being told it does not match,
                # and naming the other one would disclose which tenant holds
                # this attempt key.
                raise BudgetDenied(
                    "this attempt is already reserved for a different tenant"
                )

            stored = (
                existing["max_resource_units"],
                existing["max_runtime_seconds"],
                existing["max_cost_micros"],
            )
            if stored != requested:
                raise BudgetDenied(
                    "this attempt is already reserved with a different spend "
                    "envelope; a retry may not change the envelope it was "
                    "admitted under"
                )

            if existing["state"] == STATE_RELEASED:
                # Released means established absence — the work did not happen
                # and the budget was genuinely returned. Re-reserving under the
                # same key would spend it a second time, so this is a denial
                # and not a fresh reservation. A new attempt needs a new
                # attempt id, which is what makes the spend visible.
                raise BudgetDenied(
                    "this attempt's reservation was released; a new attempt "
                    "identity is required to reserve again"
                )

            return Reservation(
                reservation_id=existing["reservation_id"],
                job_id=job_id,
                attempt_id=attempt_id,
            )

    async def confirm(self, *, reservation: Any, envelope: Any) -> None:
        """Bind the approved envelope to this attempt. Idempotent on the same key.

        The approved envelope may legitimately differ from the requested one — an
        approval can be for less than was asked. So confirm records the approved
        values, and idempotency is "confirming the same approved envelope twice is
        a no-op", not "confirm never writes".

        A second confirm with a *different* approved envelope is denied, for the
        same reason a changed reservation is: it would be a budget increase applied
        after admission.
        """
        from harness_jobs.admission import BudgetDenied

        approved = _envelope_values(envelope)

        async with self._session() as connection:
            existing = await self._fetch_locked(
                connection, reservation.job_id, reservation.attempt_id
            )
            if existing is None:
                # Confirming something that was never reserved. A denial, not
                # an unavailable: the store answered, and the answer is that
                # this attempt holds nothing.
                raise BudgetDenied(
                    "no budget reservation exists for this attempt to confirm"
                )

            if existing["state"] == STATE_CONFIRMED:
                stored = (
                    existing["max_resource_units"],
                    existing["max_runtime_seconds"],
                    existing["max_cost_micros"],
                )
                if stored != approved:
                    raise BudgetDenied(
                        "this attempt is already confirmed with a different "
                        "spend envelope"
                    )
                return

            if existing["state"] in (STATE_RELEASED, STATE_RETAINED):
                raise BudgetDenied(
                    "this attempt's reservation is already settled and cannot "
                    "be confirmed"
                )

            await self._execute(
                connection,
                f"UPDATE {_TABLE} SET state=$1, max_resource_units=$2, "
                "max_runtime_seconds=$3, max_cost_micros=$4, updated_at=now() "
                "WHERE job_id=$5 AND attempt_id=$6",
                STATE_CONFIRMED,
                approved[0],
                approved[1],
                approved[2],
                reservation.job_id,
                reservation.attempt_id,
            )

    async def release(self, *, reservation: Any, reason: str) -> None:
        """Return a reservation as unused. Only with established absence.

        Terminal and idempotent. Releasing an already-released reservation is a
        no-op rather than an error, because the harness calls this on a
        compensation path that may itself be retried, and an exception there would
        turn a duplicate compensation into a failure.

        A *confirmed* reservation may still be released: confirm records the
        approved envelope, and absence can be established afterwards. What may not
        happen is releasing something retained — retained means "we do not know",
        and freeing budget on an unknown is the double-spend this distinction
        exists to prevent.
        """
        await self._settle(reservation, STATE_RELEASED, reason)

    async def retain(self, *, reservation: Any, reason: str) -> None:
        """Record the reservation as held pending provider reconciliation.

        Exists so that "held because we do not know" is a state the ledger was
        *told* about, rather than one an operator has to infer from a reservation
        nobody released.
        """
        await self._settle(reservation, STATE_RETAINED, reason)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _settle(self, reservation: Any, state: str, reason: str) -> None:
        from harness_jobs.admission import BudgetDenied

        text = (reason or "").strip()
        if not text:
            # The check constraint would refuse this anyway; refusing here names
            # the caller's mistake instead of surfacing a constraint violation.
            raise BudgetDenied("settling a reservation requires a reason")

        async with self._session() as connection:
            existing = await self._fetch_locked(
                connection, reservation.job_id, reservation.attempt_id
            )
            if existing is None:
                raise BudgetDenied(
                    "no budget reservation exists for this attempt to settle"
                )
            if existing["state"] == state:
                return
            if existing["state"] == STATE_RETAINED and state == STATE_RELEASED:
                raise BudgetDenied(
                    "a retained reservation is held pending reconciliation and "
                    "may not be released without establishing absence"
                )
            if existing["state"] == STATE_RELEASED:
                raise BudgetDenied("this attempt's reservation is already released")
            await self._execute(
                connection,
                f"UPDATE {_TABLE} SET state=$1, reason=$2, updated_at=now() "
                "WHERE job_id=$3 AND attempt_id=$4",
                state,
                text[:1000],
                reservation.job_id,
                reservation.attempt_id,
            )

    async def _check_limits(
        self,
        connection: Any,
        *,
        org_id: str,
        workspace_id: str,
        requested: tuple[int, int, int],
    ) -> None:
        """Deny a reservation that would exceed the workspace's configured caps.

        Sums only the states that still count as committed, so released budget is
        genuinely available again while retained budget is not.

        No limits configured means no cap to exceed, so nothing is denied. That is
        the honest reading of an unset budget and it is *not* a bypass: the caps
        this consults are the workspace's own declared limits, and inventing one
        where none is declared would deny operations in every workspace that has
        not set a budget.
        """
        if self._limits_for is None:
            return
        from harness_jobs.admission import BudgetDenied

        limits = await self._limits_for(org_id=org_id, workspace_id=workspace_id)
        if limits is None:
            return

        placeholders = ", ".join(
            f"${index}" for index in range(2, 2 + len(COMMITTED_STATES))
        )
        row = await self._fetchrow(
            connection,
            f"SELECT COALESCE(SUM(max_cost_micros), 0) AS cost, "
            f"COALESCE(SUM(max_resource_units), 0) AS units FROM {_TABLE} "
            f"WHERE workspace_id=$1 AND state IN ({placeholders})",
            workspace_id,
            *COMMITTED_STATES,
        )
        held_cost = int(row["cost"]) if row else 0
        held_units = int(row["units"]) if row else 0

        if (
            limits.max_cost_micros is not None
            and held_cost + requested[2] > limits.max_cost_micros
        ):
            # Names the dimension, never the tenant's absolute numbers: a denial
            # message reaches a caller, and a caller does not need the
            # workspace's configured ceiling to know it was exceeded.
            raise BudgetDenied(
                "this operation would exceed the workspace's configured cost budget"
            )
        if (
            limits.max_resource_units is not None
            and held_units + requested[0] > limits.max_resource_units
        ):
            raise BudgetDenied(
                "this operation would exceed the workspace's configured resource budget"
            )

    async def _insert(
        self,
        connection: Any,
        *,
        reservation_id: str,
        job_id: str,
        attempt_id: str,
        org_id: str,
        workspace_id: str,
        requested: tuple[int, int, int],
    ) -> Any:
        return await self._fetchrow(
            connection,
            f"INSERT INTO {_TABLE} (reservation_id, job_id, attempt_id, org_id, "
            "workspace_id, state, max_resource_units, max_runtime_seconds, "
            "max_cost_micros) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
            "ON CONFLICT (job_id, attempt_id) DO NOTHING RETURNING reservation_id",
            reservation_id,
            job_id,
            attempt_id,
            org_id,
            workspace_id,
            STATE_RESERVED,
            requested[0],
            requested[1],
            requested[2],
        )

    async def _fetch_locked(self, connection: Any, job_id: str, attempt_id: str) -> Any:
        """Read one reservation, holding it for the rest of the transaction.

        `FOR UPDATE` because every caller of this reads the row and then decides
        whether to write. Without the lock, two concurrent confirms could both
        observe `reserved` and both proceed, and the second would overwrite the
        first's approved envelope.
        """
        return await self._fetchrow(
            connection,
            f"SELECT reservation_id, job_id, attempt_id, org_id, workspace_id, "
            "state, max_resource_units, max_runtime_seconds, max_cost_micros "
            f"FROM {_TABLE} WHERE job_id=$1 AND attempt_id=$2 FOR UPDATE",
            job_id,
            attempt_id,
        )

    @asynccontextmanager
    async def _session(self) -> Any:
        """One connection and one transaction, with every failure classified.

        Wrapping BOTH is what makes the availability/denial split hold at the
        transport. `_acquire` alone covered only the synchronous ``self._connect()``
        call, which is almost none of what can fail: the pool is entered on
        ``__aenter__``, so a closed or unreachable pool raises *after* that try
        block, and a transaction's COMMIT happens on ``__aexit__``, after the last
        statement this class runs. Both escaped as themselves —
        `HarnessDatabaseUnavailable`, or an `asyncpg` error at commit.

        Which would be silent, and is the reason this is a context manager rather
        than a comment. The harness's `_confirm` catches `BudgetUnavailable` to
        RETAIN the reservation and `BudgetDenied` to RELEASE it; an exception that
        is neither matches no branch, so it propagates past the compensation logic
        entirely and the reservation is left in `reserved` with nothing recording
        why. A database restart between reserve and confirm is enough to reach it.

        `BudgetDenied` passes through untouched: a denial raised inside the
        transaction is this ledger's own durable answer, and translating it to
        unavailable would invite an endless retry of something already refused.
        """
        from harness_jobs.admission import BudgetDenied

        try:
            async with self._connect() as connection, _transaction(connection):
                yield connection
        except BudgetDenied:
            raise
        except Exception as error:
            raise _translate(error) from error

    async def _execute(self, connection: Any, query: str, *args: object) -> Any:
        try:
            return await connection.execute(query, *args)
        except Exception as error:
            raise _translate(error) from error

    async def _fetchrow(self, connection: Any, query: str, *args: object) -> Any:
        try:
            return await connection.fetchrow(query, *args)
        except Exception as error:
            raise _translate(error) from error


def _envelope_values(envelope: Any) -> tuple[int, int, int]:
    """The envelope's three integers, validated as integers.

    `bool` is rejected explicitly because `True` is an `int` in Python and would
    be stored as 1 — a silently-tiny envelope. `SpendEnvelope.__post_init__`
    rejects it for the same reason; this check exists because the envelope arrives
    through a Protocol and is therefore whatever the caller passed.
    """
    from harness_jobs.admission import BudgetUnavailable

    values = []
    for name in ("max_resource_units", "max_runtime_seconds", "max_cost_micros"):
        value = getattr(envelope, name, None)
        if type(value) is not int or value < 0:
            # A malformed envelope is a contract breach by the caller, not a
            # tenant's denial and not an outage. Reported as unavailable because
            # the ledger genuinely did not answer the budget question — and
            # unavailable is the conservative direction, since it retains rather
            # than releases.
            raise BudgetUnavailable(
                f"the spend envelope's {name} is not a non-negative integer"
            )
        values.append(value)
    return (values[0], values[1], values[2])


def _reservation_id(job_id: str, attempt_id: str) -> str:
    """A reservation id derived from the attempt it belongs to.

    Derived rather than random so that the identifier is reproducible from the
    idempotency key: a retry that cannot see the prior row still computes the same
    id, which means a lost reply cannot leave two differently-identified rows for
    one attempt even if the unique constraint were ever dropped. Hashed rather than
    concatenated so the id has a bounded length and does not re-encode the tenant's
    job naming into a column that appears in logs.
    """
    digest = hashlib.sha256(
        b"superplane-operation-budget\x00"
        + job_id.encode("utf-8")
        + b"\x00"
        + attempt_id.encode("utf-8")
    )
    return digest.hexdigest()


def _transaction(connection: Any) -> Any:
    """The connection's transaction context.

    Each ledger call owns its transaction. That independence is required, not
    incidental: `release` and `retain` are compensations for an admission that may
    be rolling back, and a compensation enrolled in the failing transaction rolls
    back with it — leaving budget held with nothing recording why.
    """
    return connection.transaction()


def _translate(error: BaseException) -> BaseException:
    """Map a database failure onto the ledger's two answers.

    The choice is load-bearing. Inside the harness's `_confirm`, an unavailable
    ledger **retains** the reservation while a denial **releases** it. So a
    transient outage reported as a denial would free budget for work that may be
    running.
    """
    from harness_jobs.admission import BudgetUnavailable

    sqlstate = getattr(error, "sqlstate", None)
    if sqlstate in _RETRYABLE_SQLSTATES:
        return BudgetUnavailable(
            "the budget ledger could not complete this write; it may be retried"
        )
    if isinstance(error, (BudgetUnavailable,)):
        return error
    # Never the driver's message: a constraint violation quotes the offending row,
    # and the row carries tenant identifiers.
    logger.warning(
        "the budget ledger rejected a write (%s)", type(error).__name__, exc_info=False
    )
    return BudgetUnavailable("the budget ledger could not complete this write")


def _unavailable(message: str) -> BaseException:
    from harness_jobs.admission import BudgetUnavailable

    return BudgetUnavailable(message)
