"""The store's schema, its upgrade path, and its rollback path.

Issue #5525 (w6-02), EPIC #4910, Wave 6.

## Why the DDL is here and not in the domain app's alembic tree

#5524's contract (`INTEGRATION-CONTRACT.md` §3.4) is explicit: *"A Wave 6 story that
puts an outbox table in `src/superplane-api/alembic/` has implemented shared jobs in
the domain API."* These tables are B's. They live in B's directory, they are applied
by B's migration path, and the domain app cannot reach them.

## Why plain SQL rather than an ORM migration

Three of this story's guarantees are properties of the DDL itself rather than of any
Python that runs against it:

* the duplicate refusal is a `UNIQUE` constraint, not application logic;
* the admission/outbox pair is atomic because both `INSERT`s are in one transaction
  against one database;
* the delivery claim is `FOR UPDATE SKIP LOCKED`, which has no ORM spelling that
  reads as obviously correct.

A reviewer checking those has to read the SQL. Putting it behind a model layer means
the thing to review is generated rather than written, and a generated constraint is
one nobody chose.

## Authoring is not applying

This module contains the statements and the code to run them. Nothing imports it at
process start, and merging it runs nothing: per the issue's Deployment section and
#5524 §7, no database mutation is authorized by code merge. Application happens once,
in the separately-authorized installation step (#5538, w6-15). `apply()` exists so
tests and that authorized installer have one definition to call -- not so a service
can migrate itself on boot, which is how two replicas racing a migration corrupt a
schema.

## Compatibility, stated rather than implied

`SCHEMA_VERSION` is the schema this code requires. `check_schema_version()` refuses
to operate against a schema it was not written for, in either direction:

* schema **older** than the code -> refuse. The code would reference columns that do
  not exist, and it would find that out mid-transaction.
* schema **newer** than the code -> refuse. This is the rollback case, and it is the
  one that is usually got wrong: rolling the code back while leaving the newer schema
  in place means old code writing rows that violate an invariant the new schema added
  but the old code does not know to maintain. Refusing to start is a visible outage;
  silently writing bad rows is not.

The safe rollback sequence is therefore stated as an ordering, not a script:
**roll the schema back first, then the code** -- or, preferably, only ever roll the
code back to a version whose `SCHEMA_VERSION` matches what is deployed. `DOWNGRADES`
holds the statements; running one is an authorized operational act, and it drops
rows, which is why the docstring on each says what is lost.
"""

from __future__ import annotations

from typing import Protocol

# The schema version this code is written against. Bumped by any change to
# `UPGRADES`; `check_schema_version` compares it to what the database reports.
SCHEMA_VERSION = 1


class SupportsExecute(Protocol):
    """The narrow slice of a database connection this module needs.

    A `Protocol` rather than an `asyncpg.Connection` annotation so the installer can
    pass whatever connection it already holds, and so nothing here is coupled to one
    driver. Deliberately does not include a pool, a DSN or a credential: this module
    is handed an open connection and cannot open one, which means it cannot be the
    place a connection string is read from the environment.
    """

    async def execute(self, query: str, *args: object) -> object: ...

    async def fetchval(self, query: str, *args: object) -> object: ...


# ---------------------------------------------------------------------------
# Version bookkeeping
# ---------------------------------------------------------------------------

# Its own table rather than a row in one of the operational tables: the version must
# be readable before the code trusts any other table's shape, so it cannot live in a
# table whose shape is what is in question.
_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS harness_jobs_schema_version (
    -- Single-row table. The CHECK is what makes it single-row: without it, two
    -- concurrent installers insert two rows and the version becomes ambiguous
    -- exactly when it matters most.
    id          smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),

    -- At least 1, because `current_version()` reports 0 for "not installed". Without
    -- this CHECK a stored 0 would be indistinguishable from an absent store, and the
    -- two need different responses: a fresh database should be installed, whereas a
    -- database claiming version 0 has had its bookkeeping corrupted and must not be
    -- silently re-installed over live tables. Making the ambiguous value
    -- unconstructible is cheaper than teaching every reader to disambiguate it.
    version     integer     NOT NULL CHECK (version >= 1),
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""

# ---------------------------------------------------------------------------
# Version 1
# ---------------------------------------------------------------------------

_OPERATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS harness_operations (
    -- The operation's durable identity. Supplied by the caller of the store (a
    -- UUID it minted), not generated here, so the value the caller was handed and
    -- the value stored are the same one even if the INSERT is retried.
    operation_id      text        PRIMARY KEY,

    -- The durable job this operation is work for. Third leg of the job/operation/
    -- attempt identity this story is required to persist, and the one an earlier
    -- revision of this schema omitted.
    --
    -- It is not redundant with `operation_id`, even though v1 mints exactly one job
    -- per admitted operation. The published contracts key on the *job*: the domain
    -- budget hooks are "idempotent on (job_id, attempt_id)"
    -- (`INTEGRATION-CONTRACT.md:296,368`, per #4912) and run projections read a job
    -- id too. A schema without this column forces #5526 (w6-03) either to invent a
    -- second identity or to migrate a live table to add one -- and a migration that
    -- backfills an identifier other rows are already keyed on is the expensive kind.
    -- Storing it now costs a column; omitting it costs a redesign.
    --
    -- UNIQUE encodes the v1 relationship as a constraint rather than a convention:
    -- one job, one operation. Two operations claiming the same job would make
    -- `(job_id, attempt_id)` ambiguous exactly where a budget hook must be exact --
    -- two operations' spend reserved against one key. If a later story needs many
    -- operations per job, dropping a UNIQUE is a safe migration; discovering that
    -- the invariant was never enforced is not.
    job_id            text        NOT NULL UNIQUE,

    -- The current attempt. Nullable-free at v1: an admitted operation always has a
    -- first attempt. Later attempts, leases and fences are #5527's (w6-04); the
    -- column exists now so that story adds behaviour rather than a migration that
    -- has to rewrite live rows.
    attempt_id        text        NOT NULL,

    -- Server-resolved tenant. NOT NULL and never updated after insert. These are
    -- the columns a caller must not be able to choose; `identity.py` makes them
    -- unconstructable from caller input, and nothing in `store.py` issues an UPDATE
    -- that touches them.
    org_id            text        NOT NULL,
    workspace_id      text        NOT NULL,

    -- The immutable request binding.
    action            text        NOT NULL CHECK (action IN ('provision','teardown')),
    idempotency_key   text        NOT NULL,

    -- Digest of the full request payload. This is what makes changed-payload reuse
    -- detectable: a retry recomputes the digest and the store compares. See
    -- `identity.payload_digest`.
    plan_digest       text        NOT NULL,

    -- The admitted request itself, canonically encoded (`identity.encode_payload`).
    --
    -- The digest above is one-way, which is sufficient to *detect* a changed retry
    -- and insufficient to *perform* the request. An earlier revision stored only the
    -- digest, and the consequence was precise: the `workspace_name`, `isolation_mode`
    -- and `aws_account_id` a caller supplies were unrecoverable the instant they were
    -- accepted, so a dispatcher restarting after a crash could name the operation it
    -- had to run and could not reconstruct what to run. That makes the whole
    -- durability guarantee hollow for the one case it exists for.
    --
    -- Stored beside the digest rather than instead of it, and the pair is checked on
    -- read (`store._record`): the payload must re-derive the stored digest. Storing
    -- only the payload would mean trusting whatever is in the column, and a row
    -- altered in place -- by an operator, a bad migration, or a bug in another
    -- writer -- would be executed as though it had been admitted. The digest is
    -- written once at admission and never updated, so it is the witness for the
    -- payload rather than a second copy of it.
    --
    -- Bounded by `identity`'s parameter limits before it ever reaches here
    -- (50 parameters, 16 KB total), so this column cannot be a disk-filling channel.
    request_payload   text        NOT NULL,

    -- Contract version the request was admitted under, stored rather than assumed.
    -- An operation admitted under v1 stays a v1 operation even after the store
    -- starts accepting v2, because how to interpret its parameters is a fact about
    -- when it was admitted.
    --
    -- `text` holding the contract's own spelling ('v1'), not an integer. The
    -- published constant is a string (`superplane_contracts.version`), and storing a
    -- parsed-out integer would mean this column and the wire value are two
    -- representations that have to be converted at every boundary -- which is where
    -- 'v1' and 1 stop comparing equal.
    contract_version  text        NOT NULL,

    state             text        NOT NULL,

    -- Optimistic concurrency. Every state transition requires the version the
    -- writer read, so two processes concluding the same operation differently
    -- cannot both succeed -- the second finds 0 rows updated and must re-read.
    -- A last-write-wins UPDATE would let a stale 'running' overwrite a terminal
    -- 'succeeded', which is how a finished operation gets retried.
    version           integer     NOT NULL DEFAULT 1,

    detail            text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    -- THE duplicate refusal. A UNIQUE constraint rather than a SELECT-then-INSERT,
    -- because the pre-check has a race and the constraint does not: two concurrent
    -- identical requests both find nothing and both insert, whereas here exactly
    -- one INSERT survives and the other is told why.
    --
    -- The tenant is IN the key deliberately. A derived key like `sp-aws-a100-1`
    -- carries no tenant, so a globally-unique key would let one tenant's row refuse
    -- another tenant's operation -- a cross-tenant denial of service through a name
    -- collision. Same reasoning as `provider_operations`' composite PK
    -- (`013_add_provider_operations.py:20-26`).
    CONSTRAINT harness_operations_idempotent
        UNIQUE (org_id, workspace_id, idempotency_key)
)
"""

_OPERATIONS_TENANT_INDEX = """
CREATE INDEX IF NOT EXISTS harness_operations_tenant_idx
    ON harness_operations (org_id, workspace_id, created_at DESC)
"""

_OUTBOX_TABLE = """
CREATE TABLE IF NOT EXISTS harness_dispatch_outbox (
    id              bigserial   PRIMARY KEY,

    -- One outbox row per operation, enforced. Without UNIQUE here, a retry path
    -- that re-inserted the outbox row but not the operation would produce two
    -- dispatches for one admission -- the duplicate this story exists to prevent,
    -- arriving through the queue rather than through the API.
    operation_id    text        NOT NULL UNIQUE
                        REFERENCES harness_operations (operation_id)
                        ON DELETE CASCADE,

    -- Denormalized so a delivery worker can route and scope a row without reading
    -- the operations table. That is not a performance choice: the worker is given
    -- the minimum it needs (#5525 design 3, "expose no raw database or provider
    -- secrets to workers"), and a worker that had to JOIN would need read access to
    -- every operation's full record.
    --
    -- `job_id` is here for the same reason the other three are: a worker reporting
    -- against the published `(job_id, attempt_id)` budget key must be *told* the job,
    -- not have to look it up.
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,
    job_id          text        NOT NULL,
    attempt_id      text        NOT NULL,
    action          text        NOT NULL,

    -- The request the worker has to carry out, canonically encoded. Same value as
    -- `harness_operations.request_payload`, denormalized for the reason above: the
    -- worker gets its own unit of work and no route to anything else.
    --
    -- Note what is still absent, and must stay absent: no DSN, no connection, no
    -- provider credential, no vault handle. This column holds what the *caller* asked
    -- for, which the caller already knew. Credential delivery is #5528's (w6-05), and
    -- a worker that received a secret here would be one this boundary failed to
    -- protect.
    request_payload text        NOT NULL,

    -- Delivery bookkeeping. `delivered_at IS NULL` is the pending predicate.
    --
    -- The row is marked delivered only AFTER delivery is itself durable
    -- (delete-last, per #5524 §3.3). Marking first would make a crash in between a
    -- permanent loss; marking last makes it a duplicate, and duplicates are
    -- refused by the UNIQUE constraint above. Loss is unrecoverable, duplication is
    -- already solved -- so the trade is not close.
    delivered_at    timestamptz,

    -- Claim lease. A claimed-but-unfinished row becomes claimable again once
    -- `claimed_until` passes, which is what makes delivery resumable after a worker
    -- dies holding a claim: no human has to release it.
    claimed_until   timestamptz,
    attempts        integer     NOT NULL DEFAULT 0,

    -- WHICH claim currently owns the row. Incremented by every successful claim, and
    -- required back -- unchanged -- on every settlement.
    --
    -- The lease deadline alone is not ownership. An earlier revision settled rows by
    -- id and `delivered_at` only, and the hole was reproduced against a real database:
    -- claimant A's lease expired, B legitimately took attempt 2, then A's late
    -- failure report cleared B's lease because the UPDATE matched on id alone. A third
    -- worker then claimed a row B was still actively delivering. Exhaustion had the
    -- same shape: a late verdict from a dead worker could conclude a successor's
    -- operation.
    --
    -- A monotonic generation makes staleness *decidable* instead of inferred from a
    -- clock: a settlement carries the generation it claimed under, and if the row has
    -- moved on the UPDATE matches nothing and the stale worker changes nothing. This
    -- is the same discipline as `harness_operations.version` -- show the state you
    -- read or your write is stale by definition -- and it is why neither can be
    -- replaced by comparing timestamps. Two processes' clocks disagree; a row's own
    -- counter does not.
    claim_generation bigint     NOT NULL DEFAULT 0,

    -- Set when a claim expired on the FINAL permitted attempt, which is the one state
    -- that cannot be resolved by letting the row be claimed again.
    --
    -- `attempts` is incremented at claim time, and the claim query excludes rows at
    -- the limit, so a worker that took the last attempt and died left a row that no
    -- worker may claim and whose in-process settlement died with it: pending forever,
    -- owned by nobody. Counting completed failures instead of claims would have traded
    -- that for an immortal row (a process that dies mid-delivery every time would
    -- never exhaust), so the fix is to make the abandoned final claim *settle* rather
    -- than become retryable. `recover_abandoned` writes the outcome the dead process
    -- would have written, and stamps it here so the recovery is itself durable and
    -- idempotent rather than re-derived from the clock on every pass.
    abandoned_at    timestamptz,

    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now()
)
"""

# Partial index: only undelivered rows are ever scanned by the claim query, and a
# delivered row is dead weight in an index that exists to find pending work. Keeps
# the claim cheap after the table accumulates history.
_OUTBOX_PENDING_INDEX = """
CREATE INDEX IF NOT EXISTS harness_dispatch_outbox_pending_idx
    ON harness_dispatch_outbox (id)
    WHERE delivered_at IS NULL
"""

UPGRADES: dict[int, tuple[str, ...]] = {
    1: (
        _VERSION_TABLE,
        _OPERATIONS_TABLE,
        _OPERATIONS_TENANT_INDEX,
        _OUTBOX_TABLE,
        _OUTBOX_PENDING_INDEX,
    ),
}

DOWNGRADES: dict[int, tuple[str, ...]] = {
    # Drops both operational tables and every row in them: the admission records and
    # any undelivered outbox rows. An undelivered row dropped here is work that was
    # accepted and will now never be delivered, so this is safe to run only when
    # the outbox is drained, and it is an authorized operational act rather than
    # something a process does to itself. Stated plainly because "rollback" reads as
    # safe and this one is not free.
    1: (
        "DROP TABLE IF EXISTS harness_dispatch_outbox",
        "DROP TABLE IF EXISTS harness_operations",
        "DROP TABLE IF EXISTS harness_jobs_schema_version",
    ),
}


class SchemaMismatch(RuntimeError):
    """The database's schema is not the one this code was written against.

    A `RuntimeError` rather than a `ValueError`: nothing about the caller's input is
    wrong. The deployment is inconsistent, and the only correct response is to stop.
    """


async def current_version(connection: SupportsExecute) -> int:
    """The schema version the database reports, or 0 if the store is not installed.

    0 rather than an exception for "not installed", because that is the normal state
    before the first authorized apply, and a fresh install should not have to catch
    an error to discover it is fresh.
    """
    installed = await connection.fetchval(
        "SELECT to_regclass('harness_jobs_schema_version') IS NOT NULL"
    )
    if not installed:
        return 0
    version = await connection.fetchval(
        "SELECT version FROM harness_jobs_schema_version WHERE id = 1"
    )
    return int(version) if version is not None else 0


async def apply(connection: SupportsExecute, *, target: int = SCHEMA_VERSION) -> int:
    """Bring the schema up to ``target``, returning the version now installed.

    Idempotent: every statement is `IF NOT EXISTS`, and a database already at or
    above ``target`` is left alone. Idempotent because the alternative is an
    installer that cannot be safely retried, and an installer that cannot be retried
    is one that leaves a half-applied schema when the network drops.

    Runs each version's statements in order. The caller owns the transaction: the
    authorized installer may want the whole upgrade in one, and this module should
    not decide that for it.
    """
    version = await current_version(connection)
    for step in sorted(UPGRADES):
        if version < step <= target:
            for statement in UPGRADES[step]:
                await connection.execute(statement)
            await connection.execute(
                """
                INSERT INTO harness_jobs_schema_version (id, version)
                VALUES (1, $1)
                ON CONFLICT (id) DO UPDATE
                    SET version = EXCLUDED.version, applied_at = now()
                """,
                step,
            )
            version = step
    return version


async def downgrade(connection: SupportsExecute, *, target: int) -> int:
    """Roll the schema back to ``target``, returning the version now installed.

    Destructive; see `DOWNGRADES`. Exposed as a function because the alternative is
    an operator pasting DDL under pressure, and a reviewed statement is better than
    an improvised one at that moment.
    """
    version = await current_version(connection)
    for step in sorted(DOWNGRADES, reverse=True):
        if target < step <= version:
            for statement in DOWNGRADES[step]:
                await connection.execute(statement)
            version = step - 1
    if version > 0:
        await connection.execute(
            "UPDATE harness_jobs_schema_version SET version = $1, applied_at = now()"
            " WHERE id = 1",
            version,
        )
    return version


async def check_schema_version(connection: SupportsExecute) -> None:
    """Refuse to operate against a schema this code was not written for.

    Both directions are refused, and the *newer* case is the one worth stating: it
    is what a code rollback leaves behind, and old code writing into a newer schema
    can violate an invariant the new schema added without ever failing a constraint
    it knows about. A refusal to start is visible; that is not.
    """
    version = await current_version(connection)
    if version == 0:
        raise SchemaMismatch(
            "the harness jobs store is not installed in this database. Apply schema "
            f"version {SCHEMA_VERSION} through the authorized installation step."
        )
    if version < SCHEMA_VERSION:
        raise SchemaMismatch(
            f"database schema is version {version}; this code requires "
            f"{SCHEMA_VERSION}. Apply the pending upgrade before starting."
        )
    if version > SCHEMA_VERSION:
        raise SchemaMismatch(
            f"database schema is version {version}; this code implements "
            f"{SCHEMA_VERSION}. Roll the schema back BEFORE the code, or deploy code "
            "matching the installed schema -- older code must not write into a newer "
            "schema."
        )
