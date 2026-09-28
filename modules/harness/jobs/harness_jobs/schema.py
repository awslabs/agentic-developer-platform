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

from .effects import _READ_ONLY_ACTIONS, _REMOVAL_ACTIONS
from .identity import MAX_ALLOCATION_ID_LENGTH

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
#
# 7 adds the allocation-inventory layer (#5529, w6-06): `harness_provider_report`,
# `harness_allocation_resource`, and the two completeness proofs those two cannot supply
# for themselves -- `harness_allocation_enumeration` (what the PROVIDER says it holds)
# and `harness_allocation_seal` (the allocation is closed to further membership). All
# four arrive together because a release is authorized from the four read as one answer:
# membership without a provider listing cannot show it is whole, and membership that can
# still grow cannot be released against at all.
#
# The two proofs also have to be tied to the report that relies on them, which is why
# `harness_allocation_enumeration.generation` and
# `harness_provider_report.enumeration_binding` are part of v7 rather than a later
# version: without them a listing could be replaced under a published report, so a
# LATER provider listing retroactively validated an EARLIER report -- including when the
# later listing said the handle was still present. A v7 without those columns has the
# defect, so it is not a schema this code can be asked to run against.
#
# `harness_provider_call_intent.allocation_id` is part of v7 for the third instance of
# the same reasoning. Sealing withdrew only the ability to RECORD membership, not the
# authority to CREATE, so a separately approved operation could still call the provider
# into a sealed allocation; the resource was created and billing, its membership write
# was refused by the seal, and the sealed inventory authorized releasing the budget that
# would have paid for it. Closing that needs two questions answerable at the two moments
# that matter -- "is this allocation closed?" before the provider is contacted, and "is
# any creating call against it still unaccounted for?" before it is closed -- and the
# second is a query over the call rows, which cannot be asked at all without knowing
# which allocation each call belongs to.
#
# Same reasoning a fourth time, and here the silent-skip failure has a specific cost. A
# database missing `harness_allocation_resource` cannot establish allocation membership,
# so every release assessment against it reports UNRESOLVED and no budget is ever
# returned. That is the safe direction, but it fails quietly as "nothing to release"
# rather than loudly as "the schema is behind", which is exactly what
# `check_schema_version` exists to convert into the latter.
SCHEMA_VERSION = 9


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

# ## Why a provider report is a stored row rather than a returned value
#
# Issue #5529 (w6-06). The domain's release path asks this package a question it cannot
# answer from its own records: "did this exact set of provider observations really come
# from the authenticated executor that held this operation?" A function that took the
# caller's digest and compared it to itself would answer yes to anything, which is the
# manufactured-attestation failure `provider_inventory.py:52` names outright ("Never
# merely echo the digest").
#
# So the report is COMMITTED here, by the executor, under its fence, before anyone can
# ask about it -- and the digest is recomputed from this stored payload at read time.
# That ordering is the whole property: the value being verified against was written by
# an authenticated holder at a time the verifier controls, not supplied alongside
# the question.
_PROVIDER_REPORT_TABLE = """
CREATE TABLE IF NOT EXISTS harness_provider_report (
    -- The canonical SHA-256 digest of `observations`, RECOMPUTED on every read and
    -- compared against the stored value (`inventory._verify_report`).
    --
    -- Part of the key rather than the whole of it. A digest-only key looked correct
    -- because a digest is all the domain can present, and it was wrong in both
    -- directions at once:
    --
    -- * It refused legitimate reports. Identical canonical observations are ROUTINE --
    --   a successor attempt re-querying a provider whose state has not changed
    --   produces byte-identical bytes, and unrelated operations observing unrelated
    --   resources can collide too. The first publisher owned the digest globally, so
    --   every later one was refused, and a refused publication means no attestation,
    --   which means cleanup can never be authorized. Permanently fail-closed is safe
    --   and it is also a release path that never runs.
    --
    -- * It admitted concurrent false success. Two publications of the same bytes under
    --   DIFFERENT grants both saw no row; one INSERT was discarded by
    --   `ON CONFLICT DO NOTHING` and BOTH callers were told they had succeeded, though
    --   only one attestation existed -- and the survivor was whichever committed first,
    --   not the caller being answered.
    --
    -- So identity is the full attestation binding: these bytes, attested by THIS
    -- executor, on THIS attempt, at THIS fence, for THIS operation. The domain still
    -- presents only a digest; the lookup adds the binding from the resolved grant
    -- rather than from the request, which is what stops an old grant's row from
    -- authorizing a successor's cleanup.
    report_digest   text        NOT NULL,

    -- Provenance of the operation this report is about. Deliberately NOT
    -- `ON DELETE CASCADE`: an attestation is evidence about resources that may still
    -- be billing, and evidence that disappears when an operation row is retired is
    -- evidence that was not durable. Same reasoning as
    -- `harness_allocation_resource.operation_id` below, and the same consequence if it
    -- were wrong -- a later read finds no attestation, reports unverified, and the
    -- allocation is retained rather than released. Safe, but it means operation
    -- housekeeping silently disables cleanup.
    operation_id    text        NOT NULL,

    -- Tenant, attempt and executor, copied for the reason
    -- `harness_provider_call_intent` copies them: a recovery or release pass reads this
    -- row after the operation's current attempt has moved on, so a join would report
    -- the wrong attempt and attest the wrong run.
    --
    -- `executor_id` is the resolved principal's subject -- the lease HOLDER at publish
    -- time, never a value from a request body. The domain compares it against its own
    -- authenticated submitter (`provider_handles.py:800`), so a forged value here would
    -- let one executor's report authorize another's cleanup.
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,
    attempt_id      text        NOT NULL,
    executor_id     text        NOT NULL,

    -- The fence token the publishing holder had. Retained rather than checked-and-
    -- discarded because "was this report published under authority that is still
    -- current?" is asked again at READ time, potentially minutes later and by a
    -- different process. A report published under a token that has since been
    -- superseded is refused then, not silently honoured.
    fence_token     bigint      NOT NULL CHECK (fence_token >= 1),

    -- The allocation the report is about. Derived from the approved, digest-bound plan
    -- (`inventory.allocation_id_for`), never from a worker argument -- otherwise an
    -- executor could publish a valid report naming an allocation it does not hold and
    -- collect cleanup authority over somebody else's resources.
    allocation_id   text        NOT NULL,

    -- The observation payload, in the exact canonical JSON the digest is computed over.
    -- Stored rather than only digested for the reason `harness_operations` stores its
    -- request payload: a digest answers "is this the same?" and cannot answer "what was
    -- attested?". A release dispute needs the second question answered.
    --
    -- Provider states and error details only. NOT a credential: same absence as
    -- `harness_provider_call_intent.target`, and load-bearing for the same reason --
    -- this row is written by the process that holds the provider credential.
    observations    text        NOT NULL,

    -- The sealed membership revision this report was published against, and the column
    -- that makes the report's ORDERING provable rather than merely plausible.
    --
    -- Without it, publication required only a live operation lease. An executor could
    -- therefore query the provider BEFORE creating anything, receive a truthful "the
    -- cluster is absent", publish that, then create the cluster, seal, and present the
    -- earlier report to authorize release. Every later check passed on its own: the
    -- attestation verified, the inventory was complete, the observation said ABSENT.
    -- What no check could see is that the observation was taken before the membership
    -- it was being used to clear existed -- so zero exposure was reported over a
    -- running cluster.
    --
    -- A report is evidence about the moment it was taken, and this records which moment
    -- that was in terms of the only thing that matters: the membership that was final
    -- when it was taken. `inventory.publish_report` refuses a report while the
    -- allocation is still open (there is no revision to name yet, and a list that can
    -- still grow cannot be vouched for), and `inventory._verify_report` requires this
    -- value to equal the revision the allocation is sealed over NOW. A report published
    -- against an earlier seal is therefore not merely old, it is unusable.
    sealed_revision text        NOT NULL,

    -- The provider listings this report was taken against: a digest over every
    -- `(provider, generation)` pair current for the allocation at publication
    -- (`inventory._enumeration_binding`). `sealed_revision` above proves the report
    -- came after membership was FINAL; this proves it came after the provider was last
    -- ASKED, which is a different claim and the one that was missing.
    --
    -- The gap it closes. Publication required a seal, and a seal requires a listing --
    -- so a listing always existed by then. But the listing remained replaceable
    -- afterwards, and nothing tied a report to the one that was current when it was
    -- published. So the sequence below had no check that could see it:
    --
    --   a successor publishes "the cluster is absent"     -- truthful about what it saw
    --   the successor then records its required listing   -- the provider says PRESENT
    --   a reader verifies the earlier report              -- and releases the budget
    --
    -- Every individual check passed. The seal was in force, the attestation was the
    -- successor's own, the observation said ABSENT. The later listing -- the one piece
    -- of evidence that contradicted the report outright -- made the report VALID
    -- instead of invalid, because it satisfied completeness for the very read that
    -- honoured it. Fresh evidence retroactively validating an older, contradicted
    -- report is the exact inversion of what a freshness proof is for.
    --
    -- With this column, replacing a listing advances its generation, so the binding a
    -- report carries stops matching and the report becomes unusable. The only way
    -- forward after re-asking the provider is to publish a NEW report -- and a report
    -- saying ABSENT about a handle the provider has just listed as present cannot be
    -- published, because `record_provider_enumeration` and `_reconcile` both see the
    -- handle. Fresh evidence therefore supersedes old evidence instead of rescuing it.
    enumeration_binding text    NOT NULL,

    created_at      timestamptz NOT NULL DEFAULT now(),

    -- The attestation binding. `attempt_id` and `fence_token` are IN the key, not
    -- merely stored beside it: without them a successor attempt observing unchanged
    -- provider state collides with its own predecessor's row and is refused, which is
    -- the legitimate-report case that made cleanup permanently unavailable.
    --
    -- With them, the same bytes from a different attempt are a different attestation,
    -- and `inventory._verify_report` requires every one of these columns to match the
    -- grant being verified -- so a predecessor's row is found and REJECTED rather than
    -- honoured. Two attestations of the same bytes coexisting is correct: they attest
    -- different runs, and a release dispute needs to know which run said what.
    PRIMARY KEY (
        report_digest, operation_id, org_id, workspace_id, attempt_id, executor_id,
        fence_token
    )
)
"""

# Lookup by allocation: given an allocation being released, find the reports published
# for it. Ordered by recency because a release consults the current attestation.
_PROVIDER_REPORT_ALLOCATION_INDEX = """
CREATE INDEX IF NOT EXISTS harness_provider_report_allocation_idx
    ON harness_provider_report (org_id, workspace_id, allocation_id, created_at DESC)
"""

# ## Why membership is its own table, and why it only grows
#
# `harness_provider_call_intent.provider_ref` already records "this call created
# something", which is nearly an inventory and is not one. It is one reference per
# CALL, and a single provider call routinely creates several independently billable
# things -- a cluster that brings its own disks and load balancer. Releasing budget on
# the strength of one reference per call therefore misses exactly the resources that
# keep costing money after the named one is gone, which is the "retained storage/network
# cost" case AC-01 requires evidence for.
#
# Rows are INSERTed and never deleted by this package. Membership that could shrink is
# membership that can be made to look complete by removing the inconvenient row, and
# "complete" is the flag that authorizes returning money. The domain's own persistence
# makes the same choice (`provider_handles.py:34` -- "persisted independently of
# operation rows and only grows").
#
# "Never deleted by this package" was not enough, because the DATABASE was deleting
# them. `operation_id` originally carried
# `REFERENCES harness_operations ON DELETE CASCADE`,
# which contradicts both the comment above and the consumer contract: retiring or
# deleting an operation row took its resource rows with it, leaving a still-billing
# resource unenumerated and an inventory that reads as whole. Operation retention is
# routine housekeeping; membership outliving it is the entire point of a separate table.
# So the operation is recorded as PROVENANCE -- a value, not a parent.
_ALLOCATION_RESOURCE_TABLE = """
CREATE TABLE IF NOT EXISTS harness_allocation_resource (
    -- Identity is the tenant plus the allocation plus the resource, not a serial:
    -- enumerating the same resource twice under one allocation is one member, so a
    -- retried or resumed enumeration converges instead of inflating the count. The
    -- tenant is INSIDE the key for the reason `harness_operations_idempotent` puts it
    -- there -- a provider-derived resource id contains no tenant, so a global key
    -- would let one tenant's resource name collide with another's.
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,
    allocation_id   text        NOT NULL,
    resource_id     text        NOT NULL,

    -- Which operation first enumerated this member. PROVENANCE, deliberately with no
    -- foreign key: membership must survive the removal or retirement of the operation
    -- that created it, and a `REFERENCES ... ON DELETE CASCADE` here made the database
    -- silently shrink an inventory whose whole contract is that it only grows. Kept as
    -- a plain value so "which operation established this member?" stays answerable
    -- after that operation row is gone -- which is exactly when an operator is asking.
    operation_id    text        NOT NULL,

    -- The provider's OWN durable handle for the thing, and what kind of thing it is.
    -- `provider_reference` is the value a teardown presents to the provider to ask
    -- "does this still exist?", so it must be the provider's identifier rather than a
    -- name chosen here; a locally-invented name produces a confident answer about
    -- nothing (`reconciliation.py:113` -- "A query by the wrong identity can be
    -- answered confidently and still be about the wrong resource").
    --
    -- `kind` is what makes completeness checkable across categories rather than
    -- assumed: the contract requires compute, storage AND network to be enumerated
    -- (`provider_inventory.py:3`), and an inventory of three machines and no disks is
    -- indistinguishable from a complete one without it.
    provider            text    NOT NULL,
    provider_reference  text    NOT NULL,
    kind                text    NOT NULL,

    -- The step keys this resource is attributed to. An array rather than a join table
    -- because one resource can be created by one step and later observed by another,
    -- and the domain merges these sets rather than replacing them
    -- (`provider_handles.py:889`).
    operation_keys  text[]      NOT NULL DEFAULT '{}',

    -- The fence token held when this member was enumerated, and the attempt that did
    -- it. Recorded so a stale holder's contribution is identifiable after the fact:
    -- the write itself is already refused by the fenced predicate, but an inventory
    -- read must also be able to say WHICH attempt established membership.
    attempt_id      text        NOT NULL,
    fence_token     bigint      NOT NULL CHECK (fence_token >= 1),

    created_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (org_id, workspace_id, allocation_id, resource_id)
)
"""

# Read path: one allocation's full membership. The release assessment reads every member
# of an allocation, and the primary key's leading columns already serve that prefix --
# but the operation-scoped lookup ("what did THIS operation contribute?") does not fall
# out of it, and the completeness check needs exactly that.
_ALLOCATION_RESOURCE_OPERATION_INDEX = """
CREATE INDEX IF NOT EXISTS harness_allocation_resource_operation_idx
    ON harness_allocation_resource (operation_id)
"""

# ## Why membership alone cannot prove membership is complete
#
# The original completeness rule checked that every succeeded provider call's own
# `(provider, provider_ref)` appeared in membership. That is a check that the executor
# wrote down what it was already telling us about, and it cannot detect the case that
# costs money: ONE provider call creating SEVERAL independently billed resources.
# Ask for a cluster and the provider also creates its disk and its load balancer. An
# executor that enumerates the cluster handle and omits the disk passed that check --
# the inventory read as complete, an ABSENT report on the cluster produced RELEASED
# with zero exposure, and the disk carried on billing with nothing in the system
# aware of it.
#
# Counting what the caller chose to send can never establish that the caller sent
# everything. The missing evidence has to come from the provider, so this table
# records that the executor asked the provider to ENUMERATE what it holds for the
# allocation, and what came back.
# `inventory.record_provider_enumeration` refuses the write when the
# provider names anything that is not already a member -- so the omitted disk is caught
# by the provider contradicting the executor rather than by trusting its count.
_ALLOCATION_ENUMERATION_TABLE = """
CREATE TABLE IF NOT EXISTS harness_allocation_enumeration (
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,
    allocation_id   text        NOT NULL,

    -- The provider that was asked. Per-provider rather than per-allocation because one
    -- allocation can hold resources from more than one provider, and a listing from one
    -- says nothing about another's. Completeness requires a current listing from EVERY
    -- provider that appears in membership (`inventory._completeness`); a single
    -- allocation-wide row would let one provider's answer vouch for all of them.
    provider        text        NOT NULL,

    -- The digest of the provider handles the listing returned, canonicalized the same
    -- way membership is. Compared against the handles currently enumerated for this
    -- provider, so a listing taken when membership was smaller cannot vouch for
    -- membership as it is now -- and a listing that has gone stale because something
    -- new was created reads as a mismatch rather than as a proof.
    enumerated_digest text      NOT NULL,

    -- How many handles the provider reported. Stored for the operator's benefit: a
    -- mismatch between this and the member count is the first thing worth seeing.
    handle_count    integer     NOT NULL CHECK (handle_count >= 0),

    -- Which listing this is, counted from 1 and incremented every time the row is
    -- replaced. The column that makes a listing IDENTIFIABLE rather than merely
    -- present, and it exists because a proof that can be rewritten in place is not a
    -- proof of anything.
    --
    -- A report names the listing generation it was published against
    -- (`harness_provider_report.enumeration_binding`), and verification requires that
    -- to still be current. Without a generation, two genuinely different answers from
    -- the provider were indistinguishable here -- the digest and the binding columns
    -- can both be identical across a re-listing -- so re-asking the provider silently
    -- re-validated every report taken before the question was asked again. That is the
    -- retroactive-validation defect: a successor could publish "the cluster is absent"
    -- and record its required listing afterwards, and if the listing came back saying
    -- the cluster was PRESENT the contradiction was invisible, because the earlier
    -- report was still valid and still released the budget.
    --
    -- Incrementing rather than timestamping: `recorded_at` has clock resolution and
    -- clock skew, and two listings within the same tick would compare equal. A counter
    -- advanced by the database under the allocation lock cannot.
    generation      bigint      NOT NULL DEFAULT 1 CHECK (generation >= 1),

    -- Provenance and the authority the listing was recorded under. The fence matters
    -- because a listing is only evidence about the moment it was taken: one recorded by
    -- a holder that has since been superseded is not evidence about now, and
    -- `_completeness` refuses it.
    operation_id    text        NOT NULL,
    attempt_id      text        NOT NULL,
    executor_id     text        NOT NULL,
    fence_token     bigint      NOT NULL CHECK (fence_token >= 1),

    recorded_at     timestamptz NOT NULL DEFAULT now(),

    -- One current listing per provider per AUTHORITY, not per allocation. Re-asking
    -- under the same grant replaces that grant's row (`ON CONFLICT ... DO UPDATE`) and
    -- advances its generation, because the question is always "what does the provider
    -- hold now?" and an accumulating history under one authority would let a reader
    -- pick the convenient answer.
    --
    -- The grant is IN the key because a listing is only ever evidence for the authority
    -- that took it -- `inventory._completeness` has always filtered on all four
    -- columns, so a row belonging to another attempt, holder or fence was never usable
    -- anyway.
    -- Keying on the allocation alone additionally made the rows mutually exclusive: two
    -- separately approved operations naming one allocation, or a recovery successor
    -- alongside its predecessor's record, overwrote each other, so whichever asked the
    -- provider last silently removed the other's proof and made its report
    -- unpublishable AND unrepublishable. That is a total release outage for the loser,
    -- caused by a legitimate act by an unrelated authority.
    --
    -- Keeping both rows costs a bounded number of rows per attempt and is worth having
    -- for its own sake: a release dispute wants to know which authority asked the
    -- provider what, and when.
    PRIMARY KEY (
        org_id, workspace_id, allocation_id, provider, operation_id, attempt_id,
        executor_id, fence_token
    )
)
"""

# ## Which allocation a provider call creates into
#
# Added to the v4 intent table at v7, because until v7 nothing here knew what an
# allocation was. The seal is allocation-wide, so the question "is any creating call
# against this allocation still unaccounted for?" has to be answerable from the call
# rows -- and it has to be answerable by a QUERY. The alternative was to decode every
# operation's stored request payload to find out which allocation each call belonged to,
# on the release path, under the allocation lock. That is a per-row payload decode and a
# digest verification (`store._record`) for calls that mostly are not about this
# allocation at all.
#
# Denormalized deliberately, on the same reasoning as `attempt_id` in this table: the
# value is read when the operation's current state has moved on, and the row must say
# what was true when it was written. It is copied from the approved, digest-bound plan
# (`allocation.allocation_id_for`) at the moment the intent is recorded, never from a
# worker argument -- a worker that could name its own allocation could create into a
# sealed one by naming a different one.
#
# Nullable, and that is a real answer rather than a gap: most operations name no
# allocation, and a call under such an operation is outside every inventory, so no seal
# governs it and none can release budget against it. A NOT NULL with a sentinel would
# make "no allocation" indistinguishable from "this allocation", which is the comparison
# the fence depends on.
_PROVIDER_CALL_INTENT_ALLOCATION_COLUMN = """
ALTER TABLE harness_provider_call_intent
    ADD COLUMN IF NOT EXISTS allocation_id text
"""

# Partial, because the query it serves reads only rows that HAVE an allocation, and on a
# store where most operations name none a full index would be mostly dead entries. The
# lookup is the seal-time accounting check in
# `allocation.creating_calls_unaccounted_for`, which runs while the allocation lock is
# held -- so a sequential scan here would hold the lock for the duration of a growing
# table.
_PROVIDER_CALL_INTENT_ALLOCATION_INDEX = """
CREATE INDEX IF NOT EXISTS harness_provider_call_intent_allocation_idx
    ON harness_provider_call_intent (org_id, workspace_id, allocation_id, created_at)
    WHERE allocation_id IS NOT NULL
"""

# ## Why an allocation has to be sealed before it can authorize a release
#
# `read_inventory` releases its transaction before the domain applies the result, and
# `enumerate_resources` remained permitted under the same lease afterwards. So
# membership could GROW immediately after a snapshot that had just been used to
# authorize a release:
# the answer "this allocation holds only the cluster, and the cluster is gone" was true
# when computed and false when acted on. A snapshot that authorizes returning money must
# be the last word on what the allocation contains, and nothing in a
# read-then-release-the-lock sequence can make it that.
#
# So sealing is explicit and it is a WRITE. Once an allocation is sealed, further
# membership writes are refused outright (`inventory.enumerate_resources`), and an
# allocation that is not sealed is never reported complete -- so no release can be
# authorized against a list that is still open to additions.
_ALLOCATION_SEAL_TABLE = """
CREATE TABLE IF NOT EXISTS harness_allocation_seal (
    org_id          text        NOT NULL,
    workspace_id    text        NOT NULL,
    allocation_id   text        NOT NULL,

    -- The membership revision this seal was taken over -- the same content digest
    -- `inventory._revision` computes. Re-derived on every read and compared, so a row
    -- inserted behind this package's back (a direct INSERT, a restored backup, a future
    -- writer that forgets the seal) moves the revision and the allocation reads as
    -- INCOMPLETE rather than as a sealed whole inventory. The seal is therefore a claim
    -- about specific membership, not a flag that outlives what it described.
    sealed_revision text        NOT NULL,

    -- Provenance and the authority that sealed it, on the same reasoning as the
    -- enumeration row above.
    operation_id    text        NOT NULL,
    attempt_id      text        NOT NULL,
    executor_id     text        NOT NULL,
    fence_token     bigint      NOT NULL CHECK (fence_token >= 1),

    sealed_at       timestamptz NOT NULL DEFAULT now(),

    -- One seal per allocation, and re-sealing the same membership is idempotent rather
    -- than an error (a retried seal after a transport failure converges). Re-sealing
    -- DIFFERENT membership is refused in `inventory.seal_allocation`: that is an
    -- allocation that grew after being declared final, which is the case this table
    -- exists to make impossible.
    PRIMARY KEY (org_id, workspace_id, allocation_id)
)
"""

_PROVIDER_LISTING_TABLE = """
CREATE TABLE IF NOT EXISTS harness_provider_listing (
    org_id text NOT NULL,
    workspace_id text NOT NULL,
    allocation_id text NOT NULL,
    provider text NOT NULL,
    query_id text NOT NULL CHECK (query_id ~ '^[a-f0-9]{32}$'),
    operation_id text NOT NULL,
    attempt_id text NOT NULL,
    executor_id text NOT NULL,
    fence_token bigint NOT NULL,
    generation bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('in_progress', 'completed', 'failed')),
    PRIMARY KEY (org_id, workspace_id, allocation_id, provider)
)
"""

_PROVIDER_QUERY_TABLE = """
CREATE TABLE IF NOT EXISTS harness_provider_query (
    org_id text NOT NULL,
    workspace_id text NOT NULL,
    allocation_id text NOT NULL,
    operation_id text NOT NULL,
    attempt_id text NOT NULL,
    executor_id text NOT NULL,
    fence_token bigint NOT NULL,
    observation_id text NOT NULL CHECK (observation_id ~ '^[a-f0-9]{32}$'),
    PRIMARY KEY (org_id, workspace_id, allocation_id)
)
"""

_ALLOCATION_EPOCH_TABLE = """
CREATE TABLE IF NOT EXISTS harness_allocation_epoch (
    org_id text NOT NULL,
    workspace_id text NOT NULL,
    allocation_id text NOT NULL,
    generation bigint NOT NULL DEFAULT 0,
    quarantined boolean NOT NULL DEFAULT false,
    PRIMARY KEY (org_id, workspace_id, allocation_id)
)
"""

# Mutating or unknown provider calls advance the cutoff, including same/null-handle
# settlement and recovery. A proven observation does not change provider state and
# must not invalidate the listing it is reading against. The epoch outlives calls.
_ALLOCATION_DISCOVERY_TABLE = """
CREATE TABLE IF NOT EXISTS harness_allocation_discovery (
    org_id text NOT NULL,
    workspace_id text NOT NULL,
    allocation_id text NOT NULL,
    provider text NOT NULL,
    provider_ref text NOT NULL,
    PRIMARY KEY (org_id, workspace_id, allocation_id, provider, provider_ref)
)
"""


def _read_only_sql(*, legacy=False):
    return " OR ".join(
        "(r.provider='"
        + provider
        + "' AND lower(r.operation_kind) = ANY(ARRAY["
        + ",".join("'" + kind + "'" for kind in sorted(kinds))
        + "]))"
        for provider, kinds in sorted(_READ_ONLY_ACTIONS.items())
        if not legacy or provider in {"aws", "gcp"}
    )


_ALLOCATION_EPOCH_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION harness_advance_allocation_epoch() RETURNS trigger AS $$
DECLARE r record;
BEGIN
    IF TG_OP = 'DELETE' THEN r := OLD; ELSE r := NEW; END IF;
    IF r.allocation_id IS NOT NULL AND
       NOT (__READ_ONLY__) THEN
        INSERT INTO harness_allocation_epoch
            (org_id, workspace_id, allocation_id, generation)
        VALUES (r.org_id, r.workspace_id, r.allocation_id, 1)
        ON CONFLICT (org_id, workspace_id, allocation_id)
        DO UPDATE SET generation = harness_allocation_epoch.generation + 1;
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql
""".replace(
    "__READ_ONLY__",
    _read_only_sql(),
)
_V7_ALLOCATION_EPOCH_TRIGGER_FUNCTION = _ALLOCATION_EPOCH_TRIGGER_FUNCTION.replace(
    _read_only_sql(), _read_only_sql(legacy=True)
)

_ALLOCATION_EPOCH_TRIGGER = """
CREATE OR REPLACE TRIGGER harness_provider_call_allocation_epoch
AFTER INSERT OR UPDATE OR DELETE ON harness_provider_call_intent
FOR EACH ROW EXECUTE FUNCTION harness_advance_allocation_epoch()
"""


# Bind both v6 history and rolling v6 writes before publishing version 7. The
# trigger installation takes PostgreSQL's table lock and waits for earlier writers;
# the subsequent UPDATE sees their committed calls. Later writers already run the
# trigger. A failed/interrupted backfill leaves version 6 and is safe to retry.
#
# Reproduce identity.payload_digest's length-prefixed UTF-8 hash, including Python's
# Unicode codepoint ordering (C collation over UTF-8). Never trust a JSON selector
# without checking the approved digest. JSONB rejects escaped NUL on conversion.
def _non_creating_sql(*, legacy=False):
    return " OR ".join(
        "(NEW.provider='"
        + provider
        + "' AND lower(NEW.operation_kind) = ANY(ARRAY["
        + ",".join("'" + kind + "'" for kind in sorted(kinds))
        + "]))"
        for actions in (_READ_ONLY_ACTIONS, _REMOVAL_ACTIONS)
        for provider, kinds in sorted(actions.items())
        if not legacy or provider in {"aws", "gcp"}
    )


_PROVIDER_CALL_BINDING_FUNCTION = (
    """
CREATE OR REPLACE FUNCTION harness_bind_provider_allocation() RETURNS trigger AS $$
DECLARE
    op record;
    payload jsonb;
    params jsonb;
    part text;
    joined text := '';
    pair record;
    allocation text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND
       ROW(NEW.operation_id, NEW.org_id, NEW.workspace_id, NEW.job_id,
           NEW.provider, NEW.operation_kind, NEW.target) IS DISTINCT FROM
       ROW(OLD.operation_id, OLD.org_id, OLD.workspace_id, OLD.job_id,
           OLD.provider, OLD.operation_kind, OLD.target) THEN
        RAISE EXCEPTION 'provider call binding is immutable';
    END IF;
    SELECT * INTO STRICT op FROM harness_operations
        WHERE operation_id = NEW.operation_id;
    IF ROW(NEW.org_id, NEW.workspace_id, NEW.job_id) IS DISTINCT FROM
       ROW(op.org_id, op.workspace_id, op.job_id) THEN
        RAISE EXCEPTION 'provider call tenant/job differs from approved operation';
    END IF;
    payload := op.request_payload::jsonb;
    params := payload->'parameters';
    IF jsonb_typeof(payload) IS DISTINCT FROM 'object' OR
       jsonb_typeof(params) IS DISTINCT FROM 'object' OR
       jsonb_typeof(payload->'contract_version') IS DISTINCT FROM 'string' OR
       jsonb_typeof(payload->'action') IS DISTINCT FROM 'string' OR
       jsonb_typeof(payload->'idempotency_key') IS DISTINCT FROM 'string' OR
       ROW(payload->>'contract_version', payload->>'action',
           payload->>'idempotency_key') IS DISTINCT FROM
       ROW(op.contract_version, op.action, op.idempotency_key) THEN
        RAISE EXCEPTION 'invalid approved operation payload';
    END IF;
    FOREACH part IN ARRAY ARRAY[payload->>'contract_version', payload->>'action',
                               payload->>'idempotency_key'] LOOP
        joined := joined || char_length(part)::text || ':' || part;
    END LOOP;
    FOR pair IN SELECT key, value FROM jsonb_each(params) ORDER BY key COLLATE "C"
    LOOP
        IF jsonb_typeof(pair.value) IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'approved parameters must be strings';
        END IF;
        part := pair.value #>> '{}';
        joined := joined || char_length(pair.key)::text || ':' || pair.key ||
                  char_length(part)::text || ':' || part;
    END LOOP;
    IF encode(sha256(convert_to(joined, 'UTF8')), 'hex') <> op.plan_digest THEN
        RAISE EXCEPTION 'approved operation payload digest mismatch';
    END IF;
    IF params ? 'allocation_id' THEN
        allocation := params->>'allocation_id';
        IF char_length(allocation) > __ALLOCATION_LIMIT__ OR
           btrim(allocation, __WHITESPACE__) = '' THEN
            RAISE EXCEPTION 'invalid approved allocation_id';
        END IF;
    END IF;
    IF (NEW.allocation_id IS NOT NULL AND
        NEW.allocation_id IS DISTINCT FROM allocation) OR
       (TG_OP = 'UPDATE' AND OLD.allocation_id IS NOT NULL AND
        OLD.allocation_id IS DISTINCT FROM allocation) THEN
        RAISE EXCEPTION 'provider call allocation differs from approved operation';
    END IF;
    NEW.allocation_id := allocation;
    -- Only inserts claim new creation authority. Settlements retain the immutable
    -- binding and invalidate evidence via the AFTER epoch trigger. Do not acquire
    -- the allocation lock after a v6 worker's lease/row lock on UPDATE/DELETE.
    IF allocation IS NOT NULL AND TG_OP = 'INSERT' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'harness-allocation:' || NEW.org_id || '/' || NEW.workspace_id ||
            '/' || allocation, 0));
        IF NOT (__NON_CREATING__) AND (
            EXISTS (SELECT 1 FROM harness_allocation_seal s
                    WHERE (s.org_id, s.workspace_id, s.allocation_id) =
                          (NEW.org_id, NEW.workspace_id, allocation)) OR
            EXISTS (SELECT 1 FROM harness_allocation_epoch e
                    WHERE (e.org_id, e.workspace_id, e.allocation_id) =
                          (NEW.org_id, NEW.workspace_id, allocation)
                      AND e.quarantined)
        ) THEN
            RAISE EXCEPTION 'allocation sealed or quarantined; creation refused';
        END IF;
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
""".replace("__ALLOCATION_LIMIT__", str(MAX_ALLOCATION_ID_LENGTH))
    .replace(
        "__WHITESPACE__",
        " || ".join(f"chr({code})" for code in range(0x3001) if chr(code).isspace()),
    )
    .replace(
        "__NON_CREATING__",
        _non_creating_sql(),
    )
)
_V7_PROVIDER_CALL_BINDING_FUNCTION = _PROVIDER_CALL_BINDING_FUNCTION.replace(
    _non_creating_sql(), _non_creating_sql(legacy=True)
)
_PROVIDER_CALL_BINDING_TRIGGER = """
CREATE OR REPLACE TRIGGER harness_provider_call_allocation_binding
BEFORE INSERT OR UPDATE OR DELETE ON harness_provider_call_intent
FOR EACH ROW EXECUTE FUNCTION harness_bind_provider_allocation()
"""
_PROVIDER_CALL_ALLOCATION_BACKFILL = """
UPDATE harness_provider_call_intent SET allocation_id = allocation_id
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
    7: (
        _PROVIDER_LISTING_TABLE,
        _PROVIDER_QUERY_TABLE,
        _PROVIDER_REPORT_TABLE,
        _PROVIDER_REPORT_ALLOCATION_INDEX,
        _ALLOCATION_RESOURCE_TABLE,
        _ALLOCATION_RESOURCE_OPERATION_INDEX,
        _ALLOCATION_ENUMERATION_TABLE,
        _ALLOCATION_SEAL_TABLE,
        _PROVIDER_CALL_INTENT_ALLOCATION_COLUMN,
        _PROVIDER_CALL_INTENT_ALLOCATION_INDEX,
        _ALLOCATION_EPOCH_TABLE,
        _ALLOCATION_DISCOVERY_TABLE,
        (
            "ALTER TABLE harness_allocation_enumeration ADD COLUMN IF NOT EXISTS "
            "allocation_generation bigint NOT NULL DEFAULT -1"
        ),
        _V7_ALLOCATION_EPOCH_TRIGGER_FUNCTION,
        _ALLOCATION_EPOCH_TRIGGER,
        _V7_PROVIDER_CALL_BINDING_FUNCTION,
        _PROVIDER_CALL_BINDING_TRIGGER,
        _PROVIDER_CALL_ALLOCATION_BACKFILL,
    ),
    8: (
        "ALTER TABLE harness_operation_leases "
        "ADD COLUMN IF NOT EXISTS closed_holder text, "
        "ADD COLUMN IF NOT EXISTS closed_attempt_id text",
        """CREATE TABLE IF NOT EXISTS harness_recovery_claim_bindings (
            operation_id text NOT NULL REFERENCES harness_operations(operation_id),
            fence_token bigint NOT NULL,
            org_id text NOT NULL, workspace_id text NOT NULL,
            holder text NOT NULL, attempt_id text NOT NULL, subject text NOT NULL,
            PRIMARY KEY(operation_id, fence_token)
        )""",
        """CREATE TABLE IF NOT EXISTS harness_recovery_settlements (
            receipt_id text PRIMARY KEY,
            operation_id text NOT NULL UNIQUE
                REFERENCES harness_operations(operation_id),
            org_id text NOT NULL,
            workspace_id text NOT NULL,
            job_id text NOT NULL,
            attempt_id text NOT NULL,
            claim_holder text NOT NULL,
            claim_attempt_id text NOT NULL,
            claim_fence_token bigint NOT NULL,
            payload_digest text NOT NULL CHECK (length(payload_digest)=64),
            accounting jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            delivered_at timestamptz
        )""",
        """CREATE INDEX IF NOT EXISTS harness_recovery_settlements_pending_idx
           ON harness_recovery_settlements(org_id, workspace_id, created_at)
           WHERE delivered_at IS NULL""",
        """CREATE OR REPLACE FUNCTION harness_recovery_receipt_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'recovery settlement receipts are immutable';
            END IF;
            IF (to_jsonb(NEW) - 'delivered_at') IS DISTINCT FROM
               (to_jsonb(OLD) - 'delivered_at') OR
               (OLD.delivered_at IS NOT NULL AND
                NEW.delivered_at IS DISTINCT FROM OLD.delivered_at)
            THEN RAISE EXCEPTION 'recovery settlement receipts are immutable';
            END IF;
            RETURN NEW;
        END $$""",
        "DROP TRIGGER IF EXISTS harness_recovery_receipt_immutable "
        "ON harness_recovery_settlements",
        """CREATE TRIGGER harness_recovery_receipt_immutable
           BEFORE UPDATE OR DELETE ON harness_recovery_settlements
           FOR EACH ROW EXECUTE FUNCTION harness_recovery_receipt_immutable()""",
        """CREATE TABLE IF NOT EXISTS harness_recovery_scan_cursors (
            org_id text NOT NULL,
            workspace_id text NOT NULL,
            consumer text NOT NULL,
            after_operation_id text NOT NULL DEFAULT '',
            PRIMARY KEY (org_id, workspace_id, consumer)
        )""",
    ),
    # Refresh already installed trigger bodies. Changing Python's effect map or
    # the historical v7 entry alone cannot upgrade an existing v8 database.
    9: (_ALLOCATION_EPOCH_TRIGGER_FUNCTION, _PROVIDER_CALL_BINDING_FUNCTION),
}

DOWNGRADES: dict[int, tuple[str, ...]] = {
    # Restore v8's exact action fence without deleting durable calls or evidence.
    # Drain Superplane retirement first; old code cannot execute these adapters.
    9: (_V7_ALLOCATION_EPOCH_TRIGGER_FUNCTION, _V7_PROVIDER_CALL_BINDING_FUNCTION),
    # Export and settle outstanding receipts first: removing the outbox loses the
    # durable link between a closed operation and the owning ledger's obligation.
    8: (
        "DROP TABLE IF EXISTS harness_recovery_scan_cursors",
        "DROP TABLE IF EXISTS harness_recovery_settlements",
        "DROP FUNCTION IF EXISTS harness_recovery_receipt_immutable()",
        "DROP TABLE IF EXISTS harness_recovery_claim_bindings",
        "ALTER TABLE harness_operation_leases "
        "DROP COLUMN IF EXISTS closed_holder, "
        "DROP COLUMN IF EXISTS closed_attempt_id",
    ),
    # Rolling back to v6 drops allocation membership and every provider-report
    # attestation. The hazard is the same shape as `DOWNGRADES[4]`'s and points the same
    # way: what is lost is the record of things that may still be BILLING.
    #
    # `harness_allocation_resource` is the only enumeration of an allocation's
    # independently billable resources -- the disks and load balancers a per-call
    # `provider_ref` never named. After this runs, a release assessment cannot establish
    # membership at all, so it correctly reports UNRESOLVED and retains the budget: the
    # money is not silently released, but it is also not reclaimable, because nothing
    # remains that names the resources to go and check. The failure is safe and
    # permanent, which is the better of the two directions and still not free.
    #
    # `harness_provider_report` drops the attestations. That is the subtler half: a
    # report is the evidence that a given set of provider observations came from the
    # authenticated executor, and it is keyed by its own digest. Losing it does not
    # forge anything -- a missing row is a refusal, not a pass -- but any release
    # already assessed against an attestation becomes unauditable after the fact.
    #
    # `harness_allocation_seal` and `harness_allocation_enumeration` drop the two proofs
    # that make `complete` mean anything: that the provider was asked to list what it
    # holds, and that the allocation was closed to further additions before its snapshot
    # authorized a release. Losing them is safe in the same direction -- with no seal an
    # allocation is never reported complete -- and it is the same permanence.
    #
    # The safe sequence is therefore: complete or abandon outstanding releases and
    # EXPORT all four tables first, then roll back. An inventory is not reconstructible
    # from the remaining tables (that non-reconstructibility is why they were added at
    # all) so there is no recovering this afterwards. A rollback is not a retention
    # policy.
    #
    # `harness_provider_call_intent.allocation_id` goes with them, and dropping it
    # removes the denormalized binding until a subsequent v7 upgrade backfills it
    # from the surviving approved operation. That does not reconstruct deleted
    # membership/listings/reports, so resolve outstanding cleanup before rollback.
    7: (
        (
            "DROP TRIGGER IF EXISTS harness_provider_call_allocation_binding "
            "ON harness_provider_call_intent"
        ),
        "DROP FUNCTION IF EXISTS harness_bind_provider_allocation()",
        "DROP TABLE IF EXISTS harness_provider_listing",
        "DROP TABLE IF EXISTS harness_provider_query",
        (
            "DROP TRIGGER IF EXISTS harness_provider_call_allocation_epoch "
            "ON harness_provider_call_intent"
        ),
        "DROP FUNCTION IF EXISTS harness_advance_allocation_epoch()",
        "DROP TABLE IF EXISTS harness_allocation_discovery",
        "DROP TABLE IF EXISTS harness_allocation_epoch",
        "DROP TABLE IF EXISTS harness_allocation_seal",
        "DROP TABLE IF EXISTS harness_allocation_enumeration",
        "DROP TABLE IF EXISTS harness_allocation_resource",
        "DROP TABLE IF EXISTS harness_provider_report",
        "DROP INDEX IF EXISTS harness_provider_call_intent_allocation_idx",
        "ALTER TABLE harness_provider_call_intent DROP COLUMN IF EXISTS allocation_id",
    ),
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
