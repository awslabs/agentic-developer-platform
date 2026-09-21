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

## Versions

| Version | Adds | Story |
|---|---|---|
| 1 | `harness_operations`, `harness_dispatch_outbox` | #5525 (w6-02) |
| 2 | `harness_approval_consumption` | #5526 (w6-03) |
| 3 | `harness_admission_intent` | #5526 (w6-03) repair |
| 4 | leases, provider-call intent, audit, cancellation | #5527 (w6-04) |

Rolling back to 1 has a consequence the DDL does not show: it discards the record of
which approvals have already been spent, after which replaying one admits a second
operation and reserves budget a second time against an envelope approved once. Revoke or
expire outstanding approvals **before** rolling back. See `DOWNGRADES[2]`.

Rolling back to 2 discards the record of reservations the harness is holding but has not
resolved -- see `DOWNGRADES[3]`, which is a different and narrower hazard: the money
stays held at the ledger with nothing left that knows to go and reclaim it.
"""

from __future__ import annotations

from typing import Protocol

# The schema version this code is written against. Bumped by any change to
# `UPGRADES`; `check_schema_version` compares it to what the database reports.
#
# 2 as of #5526 (w6-03), which adds `harness_approval_consumption`. See the comment
# above that table for why it is a new version rather than an addition to v1 -- in
# short, because folding a new table into an already-applied version means `apply()`
# skips it silently, and whether v1 has been applied is not something this code can
# check.
#
# 3 adds `harness_admission_intent`, for the same reason stated once more: v2 has by now
# been applied to databases this code cannot inspect, so extending v2 in place would
# make the new table's existence depend on deployment order.
#
# 4 adds the execution layer (#5527, w6-04): `harness_operation_leases`,
# `harness_provider_call_intent`, `harness_execution_audit`, and the
# cancellation-request columns on `harness_operations`. Same reasoning a third time --
# v3 has merged, so folding these into it would make them appear only on databases that
# had not yet applied v3, which is the silent-skip failure the v2 comment describes.
SCHEMA_VERSION = 6


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

# ---------------------------------------------------------------------------
# Version 2 -- approval consumption (#5526, w6-03)
# ---------------------------------------------------------------------------

# Why this is version 2 rather than an addition to version 1.
#
# #5525's README argues that the columns it added late belonged *in* v1 rather than in a
# migration, because "v1 has never been applied to any database -- there is no deployed
# schema for a migration to move". That reasoning was sound for that change and is not
# sound for this one, for two reasons:
#
#  1. This is a new table, not a NOT NULL column on an existing one. The expensive case
#     the argument was avoiding -- a backfill on live rows -- does not arise, so the
#     cheaper option it was trading against costs nothing here.
#  2. "No database has been applied" is an assumption about the world that this code
#     cannot check, and #5525 has merged. The authorized installation step (#5538,
#     w6-15) and the composition story (#5535, w6-12) are separate stories that may
#     have applied v1 by then. If they have, folding this table into v1 means `apply()`
#     sees `version >= target`, skips every statement, and the table is silently never
#     created -- and the first symptom is the approval gate failing at runtime against a
#     schema that reports itself as current.
#
# Version 2 is correct whether or not v1 was applied. Editing v1 is correct only under
# an assumption nobody can verify at the time it matters, so the choice is not close.
_APPROVAL_CONSUMPTION_TABLE = """
CREATE TABLE IF NOT EXISTS harness_approval_consumption (
    -- The approval that was consumed. PRIMARY KEY, and this single line is the whole
    -- of "an approval admits exactly once".
    --
    -- It is a constraint rather than application logic for the same reason the
    -- idempotency rule is (`harness_operations_idempotent` above): a
    -- SELECT-then-INSERT is green under test and wrong under concurrency. Two
    -- admissions racing one approval both find no consumption row and both proceed,
    -- and the effect of losing that race is a second budget reservation against an
    -- envelope a human approved once. Here the conflict IS the detection, and the
    -- loser is handed the winner's operation.
    approval_id     text        PRIMARY KEY,

    -- The operation the approval was consumed BY. This is what lets the loser of the
    -- race receive the original answer rather than an error: on conflict, the
    -- admission path reads this column and returns that operation, which is what
    -- makes an honest retry idempotent instead of merely refused.
    --
    -- No ON DELETE CASCADE, deliberately, in contrast to the outbox's reference. A
    -- cascade here would mean deleting an operation silently frees its approval for
    -- reuse -- turning a row deletion into a budget grant. RESTRICT is the safe
    -- direction: the consumption record outlives the operation, and an operator who
    -- really must remove one has to say so explicitly.
    operation_id    text        NOT NULL UNIQUE
                        REFERENCES harness_operations (operation_id)
                        ON DELETE RESTRICT,

    -- Tenant, stored rather than joined for the same reason the outbox stores it: a
    -- reader answering "was this approval consumed" must be able to scope the question
    -- without read access to the operations table.
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,

    -- What the approval was bound to, copied at consumption time. Stored so that the
    -- binding an auditor reads is the one the gate actually checked, and not whatever
    -- the approval source reports now -- an approval store that revised a record after
    -- the fact would otherwise rewrite history that money was spent on.
    plan_digest     text        NOT NULL,

    -- Who asked and who approved. Both are needed to answer "was this self-approved"
    -- after the fact, which is a question an audit asks about a spend that already
    -- happened, when the live membership data has moved on.
    requester       text        NOT NULL,
    approved_by     text        NOT NULL,

    -- The approved ceiling, as three integers rather than a serialized blob so that
    -- "what was the envelope" is answerable in SQL by an operator. Millionths, matching
    -- `SpendEnvelope.max_cost_micros`: a float column compares unequal to itself across
    -- a round trip, and comparison is the field's only purpose.
    max_resource_units  bigint  NOT NULL,
    max_runtime_seconds bigint  NOT NULL,
    max_cost_micros     bigint  NOT NULL,

    -- The reservation this admission holds against the domain ledger, and its state.
    --
    -- Recorded here rather than inferred, because the compensation rules in #5524 §3.5
    -- are decidable only if the step the sequence reached is durable. "Confirm
    -- succeeded but the commit was lost" and "reserve succeeded but confirm was lost"
    -- have different safe answers -- release after establishing nothing was dispatched,
    -- versus retry the confirm under the same key -- and a recovering process that
    -- could not tell them apart would have to guess between releasing money for work
    -- that may be running and holding money for work that never will.
    reservation_id  text,
    reservation_state text      NOT NULL
                        CHECK (reservation_state IN (
                            'reserved', 'confirmed', 'released', 'retained'
                        )),

    consumed_at     timestamptz NOT NULL DEFAULT now()
)
"""

# Lookup by operation, for the recovery and compensation paths: given an operation whose
# dispatch outcome is uncertain, find the reservation that must be retained until the
# provider is reconciled. `operation_id` is already UNIQUE, so this is the index that
# makes the reverse direction cheap rather than a second constraint.
_APPROVAL_CONSUMPTION_OPERATION_INDEX = """
CREATE INDEX IF NOT EXISTS harness_approval_consumption_operation_idx
    ON harness_approval_consumption (operation_id)
"""

# ---------------------------------------------------------------------------
# Version 3: admission intent, written before any external effect (#5526 repair)
# ---------------------------------------------------------------------------
#
# ## The hazard this table exists for
#
# Steps (1)-(3) of the admission sequence -- approve, reserve, confirm -- happen outside
# the transaction, for the reason `admission.py` gives: a transaction held open across a
# ledger call makes the store's throughput a function of the ledger's latency. The cost
# of that choice is that a process which dies between "the ledger booked a hold" and
# "the reply arrived" leaves a hold nothing in this database has ever heard of.
#
# That was reproduced (CXR-003): kill the process after `reserve` lands but before its
# reply, and a new connection sees zero operations, zero outbox rows and zero
# consumption rows, while the ledger holds budget. There was nothing to enumerate, so
# there was no reconciliation that could be written -- recovery was not "hard", it was
# undefined. And because the derived operation identity is a function of the approval
# alone, the retry could not discover the hold either: it refused on the expired
# approval before contacting the ledger at all, so the money stayed held forever.
#
# The fix is that the *intent* is durable before the first external effect. This row is
# written and committed before `reserve` is called, so the invariant becomes: a hold can
# only exist if a row here describes it. The reverse -- a row with no hold -- is the
# harmless direction, and `reconcile_interrupted_admissions` resolves it by asking the
# ledger, which is idempotent on the derived key.
#
# ## Why it is not a column on harness_approval_consumption
#
# The consumption row is written *inside* the transaction, at step (4c), and it
# references the operation. Both are exactly what this record must not do: it has to
# exist before there is an operation to reference, and it has to survive the rollback of
# a transaction that failed. A nullable set of columns on a row that does not yet exist
# is not a record.
_ADMISSION_INTENT_TABLE = """
CREATE TABLE IF NOT EXISTS harness_admission_intent (
    -- The approval this admission is spending. PRIMARY KEY, so an intent row is
    -- per-approval exactly as the consumption row is, and a retry under the same
    -- approval finds the existing row rather than writing a second one.
    --
    -- Deliberately NOT a foreign key to harness_approval_consumption: this row exists
    -- before that one does, and referencing a row that does not exist yet is the
    -- ordering problem this table was created to escape.
    approval_id     text        PRIMARY KEY,

    -- The derived identity. Stored because it IS the ledger's idempotency key: a
    -- recovering process needs to name the reservation it is asking about, and the
    -- ledger is keyed on (job_id, attempt_id) rather than on a reservation id it has
    -- not told us yet. Without these columns, recovery would have to re-derive them
    -- from the approval -- which works only while the derivation never changes, and
    -- a recovery path whose correctness depends on a pure function staying pure across
    -- versions is a recovery path that breaks silently at the worst moment.
    operation_id    text        NOT NULL UNIQUE,
    job_id          text        NOT NULL,
    attempt_id      text        NOT NULL,

    -- Tenant, so reconciliation can be scoped and so a cross-tenant read of this table
    -- is expressible as a WHERE clause rather than as a filter someone remembers to
    -- apply.
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,

    -- The approved ceiling, copied. A recovering process must be able to retry the
    -- confirm under the same envelope, and the approval it came from may by then have
    -- expired or been revoked -- which is precisely when recovery matters and precisely
    -- when the approval store can no longer answer. Copying is not duplication here:
    -- it is the difference between a reconciliation that works after expiry and one
    -- that gives up when the approval it needs to read is gone.
    max_resource_units  bigint  NOT NULL,
    max_runtime_seconds bigint  NOT NULL,
    max_cost_micros     bigint  NOT NULL,

    -- How far the sequence is known to have got. Advanced only after the corresponding
    -- external effect is known to have happened, so the column can lag reality but can
    -- never run ahead of it:
    --
    --   intended  -- nothing has been asked of the ledger yet, OR a reserve was issued
    --                and its outcome is unknown. These are one state on purpose: they
    --                are indistinguishable from inside this process after a crash, and
    --                a state that claims to distinguish them would be guessing.
    --   reserved  -- a reserve reply was received. A hold definitely exists.
    --   confirmed -- a confirm reply was received. The envelope is bound.
    --   resolved  -- the sequence reached a terminal answer (committed, refused and
    --                compensated, or reconciled). Nothing further is owed. Rows are
    --                marked rather than deleted so that "was this approval's hold ever
    --                settled, and how" stays answerable after the fact.
    stage           text        NOT NULL
                        CHECK (stage IN (
                            'intended', 'reserved', 'confirmed', 'resolved'
                        )),

    -- The reservation id, once the ledger has named one. Null while the stage is
    -- 'intended', which is the whole reason recovery keys on (job_id, attempt_id).
    reservation_id  text,

    -- How a resolved row was settled, for the operator reading after the fact. Free
    -- text rather than an enum: the useful content is which compensation ran and why,
    -- and an enum would force that into categories chosen before the incidents.
    resolution      text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
)
"""

# The reconciliation sweep's only query: unresolved rows, oldest first. A partial index
# because the interesting set is permanently tiny relative to the table -- every healthy
# admission resolves within milliseconds -- so an index over all rows would be mostly
# dead weight, and the sweep must stay cheap enough to run often. A sweep that is
# expensive gets scheduled rarely, and a reconciliation that runs rarely is a hold that
# sits for hours.
_ADMISSION_INTENT_UNRESOLVED_INDEX = """
CREATE INDEX IF NOT EXISTS harness_admission_intent_unresolved_idx
    ON harness_admission_intent (created_at)
    WHERE stage <> 'resolved'
"""

# ---------------------------------------------------------------------------
# Version 4: execution, leases, fencing and crash recovery (#5527, w6-04)
# ---------------------------------------------------------------------------
#
# ## What v1-v3 left undone
#
# By v3 an operation is durable, adjudicated, budget-bound and delivered at least once
# to an executor. What no table yet answers is **which executor is entitled to act right
# now**. `harness_dispatch_outbox.claim_generation` fences the *delivery* of an
# envelope, and deliberately nothing more: it is released the moment the envelope is
# handed over (`outbox._mark_delivered`), because its job is "this row was handed to
# someone once". The provider call happens afterwards, outside any claim, and is where
# the expensive mutations live.
#
# So the hazard v4 closes is the one the delivery claim cannot: a worker that received
# an envelope, began provisioning, and then stalled -- a long GC pause, a lost network,
# a frozen VM. Nothing stops a second worker from legitimately taking over, which is
# required for liveness, and nothing stops the first from waking up and reporting
# `succeeded` for an operation the second is still running. That report would be
# accepted by `store.transition`, because the stale worker's `version` may well still be
# current.
#
# The fence token here is what makes that decidable. It is a *separate* counter from the
# outbox's, and the separation is deliberate: they fence different things over different
# lifetimes (one envelope hand-off versus one execution attempt), and a single counter
# serving both would be released at the wrong moment for one of them.

_OPERATION_LEASES_TABLE = """
CREATE TABLE IF NOT EXISTS harness_operation_leases (
    -- One lease row per operation, for the whole of its executable life. PRIMARY KEY
    -- rather than a row per grant, because the question this table answers is
    -- "who holds it NOW" and a history table cannot answer that without a subquery that
    -- has a race in it. The history is `harness_execution_audit`, which is append-only
    -- and is the right shape for a question about the past.
    operation_id    text        PRIMARY KEY
                        REFERENCES harness_operations (operation_id)
                        ON DELETE CASCADE,

    -- Tenant, denormalized for the same reason every other table here denormalizes it:
    -- the per-tenant concurrency cap is a COUNT over this table, and a cap that had to
    -- JOIN to the operations table would need read access to every operation's full
    -- record to answer a question about how many are running.
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,

    -- THE fence token. Monotonic per operation, incremented by every grant, and never
    -- reset -- not on release, not on expiry, not on a successful completion.
    --
    -- Never reset is the whole property. A counter that restarted would hand a fresh
    -- holder a token a previous holder had already used, and the refusal rule
    -- (`superplane_contracts.leases.is_fenced_out`: refuse anything strictly below the
    -- highest seen) would then accept a stale writer whose token had come back around.
    --
    -- `bigint` because it only ever goes up. At one grant per millisecond it overflows
    -- after roughly 300 million years; at `integer` it would overflow in 25 days of the
    -- same, and a wrapped fence token is a silently accepted stale write.
    --
    -- Starts at 0 meaning "never granted", so the first grant is token 1 and
    -- `Lease.fence_token >= 1` (the contract's CHECK) holds for every real lease.
    fence_token     bigint      NOT NULL DEFAULT 0 CHECK (fence_token >= 0),

    -- The current holder, and when its entitlement lapses. Both NULL when the lease is
    -- free, and the pair is what `acquire` tests.
    --
    -- Ownership is required on release (`leases.authorize_release`): without it,
    -- freeing another worker's lease is an unauthenticated way to create the concurrent
    -- execution the lease exists to prevent.
    holder          text,
    expires_at      timestamptz,

    -- When the CURRENT holder acquired, and the runtime ceiling for this attempt. The
    -- ceiling is per-attempt rather than per-lease-period because a worker that renews
    -- forever is indistinguishable, from the outside, from one that never finishes --
    -- and "renew forever" is exactly what a healthy-but-wedged worker does. Renewal
    -- extends `expires_at` and must NOT extend this, which is what makes the cap real.
    acquired_at     timestamptz,
    runtime_deadline timestamptz,

    -- The attempt the current holder is executing. Stored so a report arriving for a
    -- previous attempt is identifiable as such even if it somehow carried a live token,
    -- and so the audit trail can be read per attempt.
    attempt_id      text,

    -- How many times this operation has been leased for execution, and the bound.
    -- Incremented at grant time, like `harness_dispatch_outbox.attempts` and for the
    -- same reason: a counter advanced at hand-out time cannot be skipped by a process
    -- that dies before recording that it tried, whereas one advanced at completion
    -- makes a repeatedly-crashing operation immortal.
    attempts        integer     NOT NULL DEFAULT 0,

    -- Set when the lease will never be granted again: the operation reached a terminal
    -- state, or execution attempts were exhausted. A closed lease is not acquirable,
    -- and this is a durable stamp rather than an inference from the operation's state
    -- so that "may this be executed" is answerable from this table alone -- the
    -- predicate the concurrency cap and the recovery sweep both need, without either
    -- having to re-derive terminality.
    closed_at       timestamptz,
    closed_reason   text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- A held lease must name its holder, its expiry, its attempt and its deadline; a
    -- free one must name none of them. Enforced rather than assumed because a row with
    -- an expiry and no holder would be acquirable-but-not-expired -- a lease nobody
    -- holds and nobody may take, which is a stuck operation needing an operator.
    CONSTRAINT harness_operation_leases_held_consistently CHECK (
        (holder IS NULL AND expires_at IS NULL
            AND acquired_at IS NULL AND runtime_deadline IS NULL
            AND attempt_id IS NULL)
        OR
        (holder IS NOT NULL AND expires_at IS NOT NULL
            AND acquired_at IS NOT NULL AND runtime_deadline IS NOT NULL
            AND attempt_id IS NOT NULL)
    )
)
"""

# The per-tenant concurrency cap's query: live leases for one tenant. Partial, because
# the cap counts only leases that are held and open -- which is a small fraction of the
# table once history accumulates, and the cap is evaluated on every acquisition.
_OPERATION_LEASES_TENANT_INDEX = """
CREATE INDEX IF NOT EXISTS harness_operation_leases_tenant_idx
    ON harness_operation_leases (org_id, workspace_id)
    WHERE holder IS NOT NULL AND closed_at IS NULL
"""

# The recovery sweep's query: open leases that have lapsed, oldest first. Also partial,
# and for the reason `harness_admission_intent_unresolved_idx` gives -- a sweep that is
# expensive gets scheduled rarely, and a recovery that runs rarely is a resource left
# running and billing.
_OPERATION_LEASES_EXPIRED_INDEX = """
CREATE INDEX IF NOT EXISTS harness_operation_leases_expired_idx
    ON harness_operation_leases (expires_at)
    WHERE holder IS NOT NULL AND closed_at IS NULL
"""

# ## Why the provider call needs its own intent table
#
# `harness_admission_intent` (v3) makes the LEDGER call recoverable: a row exists before
# `reserve` is issued, so a hold can only exist if a row describes it. The provider call
# has exactly the same structure and exactly the same hazard -- a process that dies
# between "the provider created the VPC" and "the reply arrived" leaves capacity nothing
# in this database has heard of -- and it is the more expensive of the two, because the
# thing left behind is billable rather than merely reserved.
#
# It is a separate table rather than more columns on the admission intent because the
# two have different lifetimes and different cardinality: admission intent is one row
# per approval, written once before anything is spent, and resolved within milliseconds.
# A provider-call intent is one row per provider mutation per attempt -- an attempt may
# make several, and a retried operation makes more -- and it is written under a fence
# token the admission intent knows nothing about.
_PROVIDER_CALL_INTENT_TABLE = """
CREATE TABLE IF NOT EXISTS harness_provider_call_intent (
    -- The idempotency key this call will present to the provider. PRIMARY KEY, so
    -- recording the intent to make a call is itself the claim on that key: two workers
    -- cannot both believe they are about to issue the same provider call, because the
    -- second INSERT conflicts.
    --
    -- Caller-supplied rather than generated here, and deliberately so: the value must
    -- be derivable by a RECOVERING process that has only the operation and attempt to
    -- go on. A generated key would be unknown after a crash, which is precisely the
    -- moment it is needed -- the provider must be asked about the call under the same
    -- key it was made under, or the question is about a different call.
    idempotency_key text        PRIMARY KEY,

    operation_id    text        NOT NULL
                        REFERENCES harness_operations (operation_id)
                        ON DELETE CASCADE,

    -- Tenant and identity, copied. `attempt_id` is here rather than joined because the
    -- operation's current attempt will have MOVED ON by the time a recovery pass reads
    -- this row -- that is what recovery means -- so a join would report the wrong
    -- attempt and reconcile the wrong call.
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,
    job_id          text        NOT NULL,
    attempt_id      text        NOT NULL,

    -- The fence token the calling worker held. This is what makes a late resolution
    -- refusable: a worker resolving an intent must still hold the token it recorded,
    -- and one whose lease has since been granted to someone else does not.
    fence_token     bigint      NOT NULL CHECK (fence_token >= 1),

    -- What the call was going to do, in the provider's own terms, and the target.
    -- Stored for the operator reading an unresolved row at 3am: "an unresolved call
    -- exists" is not actionable, "a create_vpc against account X under key K is
    -- unresolved" is.
    --
    -- NOT a credential, NOT a connection string, NOT a vault handle. The same absence
    -- the outbox's `request_payload` comment states, restated because this table is
    -- written by the executor and an executor is the process that HAS the credential --
    -- so this is the boundary where one would most plausibly be logged. Credential
    -- delivery is #5528's (w6-05); nothing about a secret is recordable here.
    provider        text        NOT NULL,
    operation_kind  text        NOT NULL,
    target          text        NOT NULL,

    -- How far this call is KNOWN to have got. Same discipline as
    -- `harness_admission_intent.stage`: advanced only after the corresponding external
    -- effect is known to have happened, so it may lag reality and must never run ahead.
    --
    --   intended   -- committed, and the provider has NOT been called, OR was called
    --                 and the outcome is unknown. One state for both on purpose: after
    --                 a crash they are indistinguishable from inside this process, and
    --                 a state claiming to tell them apart is a guess recorded as fact.
    --   observed   -- a provider reply was received and recorded in `outcome`.
    --   reconciled -- a recovery pass asked the provider what happened and got an
    --                 answer. Distinct from `observed` because "the original caller saw
    --                 this" and "we went back and asked" are different provenance for
    --                 the same fact, and an auditor of a duplicated spend needs to know
    --                 which one a row is.
    --   unresolved -- the provider could not be reached to answer. Terminal for this
    --                 row and NOT a failure: it is the state in which budget is
    --                 retained and a human decides. It exists because "we asked and it
    --                 said no" and "we could not ask" have opposite safe answers, and a
    --                 schema without a place for the second forces it to be written as
    --                 the first.
    stage           text        NOT NULL
                        CHECK (stage IN (
                            'intended', 'observed', 'reconciled', 'unresolved'
                        )),

    -- The provider's answer, once there is one. Free text: the useful content is the
    -- resource identifier or the provider's error, and an enum would force that into
    -- categories chosen before the incidents.
    outcome         text,

    -- The provider-side identifier, when the call created something. Separate from
    -- `outcome` because this is the value a teardown needs in order to release what a
    -- reconciled-but-uncertain provision may have left standing, and digging it out of
    -- free text at that moment is how a cleanup misses a resource.
    provider_ref    text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
)
"""

# The sweep's query: calls that may have happened and have never been resolved, oldest
# first. `intended` only -- the other three stages are settled, and `unresolved` in
# particular must NOT be swept again automatically: it is the state that says a human
# decides, and a sweep that kept retrying it would be overriding that decision on a
# timer.
_PROVIDER_CALL_INTENT_UNRESOLVED_INDEX = """
CREATE INDEX IF NOT EXISTS harness_provider_call_intent_unresolved_idx
    ON harness_provider_call_intent (created_at)
    WHERE stage = 'intended'
"""

# Lookup by operation and attempt, for the report and cleanup paths: given an operation
# whose outcome is uncertain, find every provider call made on its behalf.
_PROVIDER_CALL_INTENT_OPERATION_INDEX = """
CREATE INDEX IF NOT EXISTS harness_provider_call_intent_operation_idx
    ON harness_provider_call_intent (operation_id, attempt_id)
"""

# ## The audit trail
#
# The issue requires "durable audit without raw credentials". Append-only, and separate
# from every table above, because the tables above are *current state* -- they are
# UPDATEd, so they cannot answer "what did the system do, in order". A stale worker's
# refused write is invisible in current state by construction (it changed nothing), and
# that refusal is exactly the event an incident review needs to see.
_EXECUTION_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS harness_execution_audit (
    id              bigserial   PRIMARY KEY,

    -- No foreign key to harness_operations, deliberately, and it is the one place here
    -- that omits it. The audit record must outlive its subject: a cascade would mean
    -- deleting an operation erases the record of what was spent on it, which turns a
    -- row deletion into the destruction of the evidence. ON DELETE RESTRICT would be
    -- the other option and is worse -- it makes the audit trail block cleanup, so the
    -- pressure becomes to delete the audit.
    operation_id    text        NOT NULL,

    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,
    attempt_id      text,

    -- The fence token in force for the event, when one applies. Nullable because some
    -- events (a refused acquisition, a cancellation request from a user) happen when
    -- the actor holds no token at all -- and recording a 0 there would be
    -- indistinguishable from a real token in a comparison.
    fence_token     bigint,

    -- What happened, and to whom. `actor` is a resolved principal or a worker identity,
    -- never a credential.
    event           text        NOT NULL,
    actor           text        NOT NULL,

    -- Whether the actor's request was allowed. The refusals are the valuable half of
    -- this table: a fenced-out worker's attempt to publish success leaves no trace
    -- anywhere else, because refusing it correctly means changing nothing.
    allowed         boolean     NOT NULL,

    -- Free-text context. Bounded by the callers rather than by a column type, and
    -- carrying no secret: same absence as `harness_provider_call_intent.target`, and
    -- more load-bearing here because an audit writer is the code most tempted to
    -- "log everything for debugging".
    detail          text,

    recorded_at     timestamptz NOT NULL DEFAULT now()
)
"""

# Read path: one operation's history in order. The audit is written on every execution
# event, so this index is what keeps "show me what happened to this operation" from
# scanning a table that grows with total platform activity.
_EXECUTION_AUDIT_OPERATION_INDEX = """
CREATE INDEX IF NOT EXISTS harness_execution_audit_operation_idx
    ON harness_execution_audit (operation_id, id)
"""

# ## Cancellation, as columns rather than a state
#
# `OperationState` is fixed by a contract shared with the domain app
# (`superplane_contracts.provisioning.OperationState`), and
# `tests/test_contract_agreement` asserts the two spellings agree. Adding a
# `cancel_requested` member here would break that agreement, and the drift test is the
# thing that would catch it -- so the state machine is not where this belongs.
#
# It is also not what is true. A cancellation request does not replace the operation's
# state: an operation that is RUNNING and has a cancellation pending is *both*, and the
# executor needs both facts to decide what to do at its next safe point. Collapsing them
# into one column would lose the information that work is still in flight -- which is
# the information that decides whether the budget may be released or must be retained.
_OPERATIONS_CANCELLATION_COLUMNS = """
ALTER TABLE harness_operations
    ADD COLUMN IF NOT EXISTS cancel_requested_at timestamptz,
    ADD COLUMN IF NOT EXISTS cancel_requested_by text,
    ADD COLUMN IF NOT EXISTS cancel_reason       text
"""

_RECONCILIATION_RETRY_COLUMNS = """
ALTER TABLE harness_provider_call_intent
    ADD COLUMN IF NOT EXISTS reconcile_attempts integer NOT NULL DEFAULT 0
        CHECK (reconcile_attempts >= 0),
    ADD COLUMN IF NOT EXISTS reconcile_after timestamptz;
ALTER TABLE harness_operations
    ADD COLUMN IF NOT EXISTS cleanup_required boolean NOT NULL DEFAULT false
"""

UPGRADES: dict[int, tuple[str, ...]] = {
    1: (
        _VERSION_TABLE,
        _OPERATIONS_TABLE,
        _OPERATIONS_TENANT_INDEX,
        _OUTBOX_TABLE,
        _OUTBOX_PENDING_INDEX,
    ),
    2: (
        _APPROVAL_CONSUMPTION_TABLE,
        _APPROVAL_CONSUMPTION_OPERATION_INDEX,
    ),
    3: (
        _ADMISSION_INTENT_TABLE,
        _ADMISSION_INTENT_UNRESOLVED_INDEX,
    ),
    4: (
        _OPERATION_LEASES_TABLE,
        _OPERATION_LEASES_TENANT_INDEX,
        _OPERATION_LEASES_EXPIRED_INDEX,
        _PROVIDER_CALL_INTENT_TABLE,
        _PROVIDER_CALL_INTENT_UNRESOLVED_INDEX,
        _PROVIDER_CALL_INTENT_OPERATION_INDEX,
        _EXECUTION_AUDIT_TABLE,
        _EXECUTION_AUDIT_OPERATION_INDEX,
        _OPERATIONS_CANCELLATION_COLUMNS,
    ),
    5: (_RECONCILIATION_RETRY_COLUMNS,),
    6: (
        """ALTER TABLE harness_operation_leases
           ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 5
           CHECK (max_attempts > 0)""",
    ),
}

DOWNGRADES: dict[int, tuple[str, ...]] = {
    6: ("ALTER TABLE harness_operation_leases DROP COLUMN IF EXISTS max_attempts",),
    # Drain reconciliation and resolve pending cleanup before removing their evidence.
    5: (
        """
        ALTER TABLE harness_provider_call_intent
            DROP COLUMN IF EXISTS reconcile_attempts,
            DROP COLUMN IF EXISTS reconcile_after;
        ALTER TABLE harness_operations DROP COLUMN IF EXISTS cleanup_required
    """,
    ),
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
    # Rolling back to v1 drops every record of which approvals have already been
    # consumed. That is not a neutral cleanup: after this runs, an approval that already
    # admitted an operation and already reserved budget is indistinguishable from an
    # unused one, so replaying it admits a second operation and reserves a second time
    # against an envelope a human approved once.
    #
    # Dropped rather than retained anyway, because the alternative is worse in a
    # different direction: leaving the table while the code that maintains it is gone
    # means v1 code admitting operations that never record a consumption, so the rows
    # that survive are a partial record indistinguishable from a complete one.
    #
    # The safe sequence is therefore: revoke or expire outstanding approvals FIRST, then
    # roll back. Stated here because `downgrade()` exists so an operator runs a reviewed
    # statement rather than an improvised one, and this is the part of it that is not
    # obvious from the DDL.
    2: ("DROP TABLE IF EXISTS harness_approval_consumption",),
    # Rolling back to v2 discards the record of which reservations the harness is
    # holding and has not yet settled. The hazard is narrower than `DOWNGRADES[2]`'s and
    # runs the other way: nothing becomes *reusable*, but any hold still outstanding
    # becomes unreclaimable -- the ledger keeps the budget, and the only record naming
    # the (job_id, attempt_id) needed to ask about it is gone. The money does not come
    # back on its own, because a reservation is released by someone deciding to release
    # it.
    #
    # The safe sequence is therefore: run `reconcile_interrupted_admissions` until it
    # reports nothing outstanding, THEN roll back. Unlike the v2 rollback there is no
    # counter-argument for keeping the table -- v2 code simply never reads it, so a
    # surviving table would be an inert set of rows slowly diverging from the ledger,
    # which is worse than absent because it looks authoritative.
    3: ("DROP TABLE IF EXISTS harness_admission_intent",),
    # Rolling back to v3 discards the execution layer, and the hazard is the worst of
    # the four -- worse than DOWNGRADES[3]'s unreclaimable hold, because what is lost
    # here is the record of things that may be RUNNING.
    #
    # Dropping `harness_provider_call_intent` destroys every record of a provider call
    # whose outcome was never established. Those rows are the only thing naming the
    # idempotency key a reconciliation must ask the provider about, so after this runs,
    # a VPC or a cluster that was created by a call nobody heard the reply to is
    # unreconcilable: it keeps running, it keeps billing, and nothing in this database
    # knows it might exist. Unlike a reserved-but-unconfirmed hold, that is real money
    # against a real resource rather than a bookkeeping entry.
    #
    # Dropping `harness_operation_leases` resets every fence token to "never granted".
    # That is the subtler half and it is not merely lost history: a worker still holding
    # token 7 from before the rollback becomes acceptable again the moment v4 is
    # re-applied and a new grant issues token 1..7, because the refusal rule compares
    # against the highest token the row knows about -- and the row now knows about none.
    # The rollback therefore un-fences workers that were correctly fenced out. This is
    # exactly the counter-reset the table's `fence_token` comment says must never
    # happen, reachable through the schema rather than through the code.
    #
    # The safe sequence is therefore: drain execution to a standstill and confirm no
    # `intended` provider-call intents remain (`recovery.list_unresolved_provider_calls`
    # reports nothing), THEN roll back. If any remain, reconcile or accept them as
    # abandoned spend FIRST -- there is no recovering them afterwards.
    #
    # `harness_execution_audit` is dropped with the rest, which deletes the record of
    # who did what. Stated plainly rather than quietly: if the audit trail is needed for
    # an incident review, export it before rolling back, because a rollback is not a
    # retention policy.
    #
    # The cancellation columns are dropped last. A pending-but-unhonoured cancellation
    # becomes invisible, so an operation a user asked to stop will continue to
    # completion under v3 code that cannot see the request.
    4: (
        "DROP TABLE IF EXISTS harness_execution_audit",
        "DROP TABLE IF EXISTS harness_provider_call_intent",
        "DROP TABLE IF EXISTS harness_operation_leases",
        """
        ALTER TABLE harness_operations
            DROP COLUMN IF EXISTS cancel_requested_at,
            DROP COLUMN IF EXISTS cancel_requested_by,
            DROP COLUMN IF EXISTS cancel_reason
        """,
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
