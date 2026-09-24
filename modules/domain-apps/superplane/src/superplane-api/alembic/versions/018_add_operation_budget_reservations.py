"""Domain-owned budget reservations for admitted operations (#5535, W6, EPIC #4910).

WHY THIS TABLE IS THE DOMAIN'S AND NOT THE HARNESS'S

`harness_jobs` declares `BudgetLedger` as a Protocol and implements none of it, on
purpose: `modules/harness/jobs/tests/test_admission_bypass.py:221`
(`test_this_module_implements_no_ledger`) FAILS if that package ever ships a
concrete ledger. Budget authority is the domain's, because the domain is what
knows a workspace's limits and what it has already spent. So the absence of a
ledger in the shared package is not a blocker for this story — it is an
instruction to this story. `OperationFacadeService.__post_init__` raises
`ContractViolation` when `ledger is None`, which means the facade cannot be
composed at all until this table exists.

## Why a table, and not an in-memory or recomputed answer

The property required of the ledger is idempotency on `(job_id, attempt_id)`
across process restarts (`harness_jobs/admission.py:391-436`). The failure it
exists to survive is a retry whose first attempt's reply was lost: the caller
retries, and if the ledger has no durable memory of the first reservation it
reserves a second time and the retry becomes a budget increase. An in-memory
ledger answers correctly until the process restarts, which is exactly when the
retry arrives.

## The columns, and which defect each one answers

- `job_id`, `attempt_id` — the idempotency key, and the UNIQUE constraint over the
  pair is what makes a second reservation under the same key impossible rather
  than merely unlikely. Enforced by the database because the concurrent case is
  two replicas reserving simultaneously, where an application-level
  "SELECT then INSERT" has a window between the two statements and a unique index
  does not. `reservation_id` is the surrogate the harness's `Reservation` carries
  back; the pair is the real identity.
- `max_resource_units`, `max_runtime_seconds`, `max_cost_micros` — the envelope, as
  three `BigInteger` columns mirroring `harness_jobs.approval.SpendEnvelope` field
  for field. `BigInteger` and not `Numeric`: the envelope is denominated in micros
  precisely so that money is integer arithmetic, and storing it as a scaled decimal
  would reintroduce the rounding the micros denomination removes. They are stored
  rather than only digested because a repeat under the same key with a CHANGED
  envelope must be refused, and refusing it requires knowing what the first one
  was. A digest alone would establish inequality without being able to say what
  changed, and an operator reading a denial needs that.
- `state` — `reserved` -> `confirmed` | `released` | `retained`, constrained to
  exactly those four. The vocabulary agrees with the harness's own
  `harness_approval_consumption.reservation_state` check constraint, so the two
  sides of the seam cannot disagree about what a state means. `retained` is
  separate from `released` and that distinction is the point: released means
  established absence, retained means "held because we do not know", and
  collapsing them either leaks budget or frees budget for spend that may have
  happened.
- `org_id`, `workspace_id` — the tenant. NOT NULL and constrained non-blank so an
  unattributable reservation cannot exist; a reservation nobody can attribute is a
  reservation no limit applies to.
- `reason` — why a reservation stopped being claimable. Required for `released` and
  `retained`, and NULL for `reserved` and `confirmed`, enforced by
  `ck_..._reason_when_settled` below. The split follows the Protocol exactly:
  `release` and `retain` each take a `reason`, `confirm` takes only an envelope, so
  `confirmed` has no reason to record and demanding one would force the adapter to
  invent it. `released` and `retained` are the two states an operator finds later
  and needs explained.
- `created_at`, `updated_at` — for the operator diagnosing a stranded reservation.
  Deliberately not used to expire anything: age cannot distinguish a dead attempt
  from a slow one, and a time-based auto-release would free budget for work that is
  still running. Stranded reservations are settled by the harness's recovery path
  or by an operator, never by a clock.

## What this migration does NOT do

It creates no foreign key to `workspaces` or `organizations`. A reservation is
taken during admission of an operation that may be the very thing creating the
workspace, so a constraint requiring the workspace row to already exist would
refuse exactly the first reservation in a workspace's life. The tenant columns are
text for the same reason `017`'s are: the identity admitted upstream is a
non-blank string, and a narrower column would fail at INSERT for an id the gates
accepted.

It does not name any table `harness_*`. Those sixteen tables are created by
`harness_jobs.schema.apply` and are owned by that package;
`harness_jobs/schema.py:8` records that putting them in this component's Alembic
chain would mean the domain API had implemented shared jobs.
"""

import sqlalchemy as sa

from alembic import op

revision = "018_add_operation_budget_reservations"
down_revision = "017_add_workspace_bootstrap_reservations"
branch_labels = None
depends_on = None

_TABLE = "operation_budget_reservations"

# Module constants so `upgrade` and `downgrade` cannot drift: a downgrade that
# drops a constraint by a misspelled name fails at the moment someone needs it.
_KEY_UNIQUE = "uq_operation_budget_reservations_attempt"
_STATE_CHECK = "ck_operation_budget_reservations_state"
_RESERVATION_CHECK = "ck_operation_budget_reservations_reservation_id"
_JOB_CHECK = "ck_operation_budget_reservations_job_id"
_ATTEMPT_CHECK = "ck_operation_budget_reservations_attempt_id"
_ORG_CHECK = "ck_operation_budget_reservations_org_id"
_WORKSPACE_CHECK = "ck_operation_budget_reservations_workspace_id"
_ENVELOPE_CHECK = "ck_operation_budget_reservations_envelope_non_negative"
_REASON_CHECK = "ck_operation_budget_reservations_reason_when_settled"
_WORKSPACE_STATE_INDEX = "ix_operation_budget_reservations_workspace_state"
_ORG_INDEX = "ix_operation_budget_reservations_org_id"


def upgrade():
    op.create_table(
        _TABLE,
        sa.Column("reservation_id", sa.String(128), primary_key=True),
        sa.Column("job_id", sa.String(255), nullable=False),
        sa.Column("attempt_id", sa.String(255), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("max_resource_units", sa.BigInteger(), nullable=False),
        sa.Column("max_runtime_seconds", sa.BigInteger(), nullable=False),
        sa.Column("max_cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # THE idempotency constraint. Everything else in this table is bookkeeping;
        # this is the property the Protocol demands. Two replicas reserving the same
        # attempt concurrently both reach INSERT, and exactly one succeeds.
        sa.UniqueConstraint("job_id", "attempt_id", name=_KEY_UNIQUE),
        # The same four states the harness's own `harness_approval_consumption`
        # constrains, spelled out rather than imported: a migration must render the
        # same DDL forever, independent of what a later revision of either package
        # happens to define.
        sa.CheckConstraint(
            "state IN ('reserved', 'confirmed', 'released', 'retained')",
            name=_STATE_CHECK,
        ),
        # Blank-but-present is the partial-record failure the Python also refuses
        # (`Reservation.__post_init__` raises `ContractViolation` for a blank field).
        # POSIX character class, not `btrim`: bare `btrim` trims spaces only, while
        # `str.strip()` trims all whitespace, so a tab-only id would be refused by
        # the code and accepted by the database. Written as a class rather than with
        # escaped control characters because this DDL is also RENDERED in Alembic
        # offline mode, where a literal tab would split the statement.
        sa.CheckConstraint(
            "reservation_id !~ '^[[:space:]]*$'", name=_RESERVATION_CHECK
        ),
        sa.CheckConstraint("job_id !~ '^[[:space:]]*$'", name=_JOB_CHECK),
        sa.CheckConstraint("attempt_id !~ '^[[:space:]]*$'", name=_ATTEMPT_CHECK),
        sa.CheckConstraint("org_id !~ '^[[:space:]]*$'", name=_ORG_CHECK),
        sa.CheckConstraint("workspace_id !~ '^[[:space:]]*$'", name=_WORKSPACE_CHECK),
        # `SpendEnvelope.__post_init__` rejects negatives and rejects `bool`. The
        # database cannot check the type, but it can make a negative envelope
        # unrepresentable — and a negative `max_cost_micros` in a SUM() is a budget
        # increase disguised as a reservation.
        sa.CheckConstraint(
            "max_resource_units >= 0 AND max_runtime_seconds >= 0 "
            "AND max_cost_micros >= 0",
            name=_ENVELOPE_CHECK,
        ),
        # A reservation SETTLED BY A COMPENSATION must say why. Stated as an
        # implication rather than a plain NOT NULL because neither `reserved` nor
        # `confirmed` has a reason to give.
        #
        # `confirmed` is in the exempt list deliberately, and getting that wrong is
        # how this constraint made `confirm` impossible: the `BudgetLedger` Protocol
        # gives `release` and `retain` a `reason` parameter and gives `confirm` only
        # an envelope, so there is no reason to write at confirm — the adapter's
        # UPDATE sets state and the approved envelope and nothing else. An earlier
        # revision of this migration exempted only `reserved`, which meant every
        # confirm violated the check and was reported as `BudgetUnavailable`. That is
        # the worst available failure: the harness's `_confirm` RETAINS on
        # unavailable, so every admitted operation would have held its budget
        # forever while the readout said the database was unwell.
        #
        # The narrower reading is also the correct one. A reason explains why a
        # reservation stopped being claimable, and `confirmed` is not that — it is
        # the reservation being honoured. `released` and `retained` are the two
        # states an operator finds later and needs explained.
        sa.CheckConstraint(
            "state IN ('reserved', 'confirmed') "
            "OR (reason IS NOT NULL AND reason !~ '^[[:space:]]*$')",
            name=_REASON_CHECK,
        ),
    )
    # The limit query: "what does this workspace currently hold against its
    # budget", which sums `max_cost_micros` over the states that still count as
    # committed. Indexed on the pair because filtering by workspace alone would
    # scan every settled reservation the workspace has ever had.
    op.create_index(_WORKSPACE_STATE_INDEX, _TABLE, ["workspace_id", "state"])
    # Tenant-scoped operator reads: "what is this org currently holding".
    op.create_index(_ORG_INDEX, _TABLE, ["org_id"])


def downgrade():
    op.drop_index(_ORG_INDEX, table_name=_TABLE)
    op.drop_index(_WORKSPACE_STATE_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
