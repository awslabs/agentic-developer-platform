"""Durable bootstrap reservations for workspace bootstrap (#5533, w6-10, EPIC #4910).

WHAT THIS TABLE IS FOR, AND WHY IT IS A TABLE

`workspace_bootstrap/superplane_bootstrap/registry.py` (`SqlRegistrationStore`) issues
every statement in this schema, and until this migration those statements named a table
no migration created. That is review finding F11: the store was implemented and tested
against a scripted double, so the SQL compiled and nothing had ever checked it against a
real schema.

The reservation is a ROW rather than an in-memory claim because the failure it exists to
survive is a process that DIES between clearing the node taint and writing the
registration (review finding F5). A claim held in memory is gone at exactly the moment it
is needed; a row in state `reserved` after the taint was cleared is the durable evidence
that lets `recover_interrupted_bootstrap` tell an interrupted bootstrap from a fresh one.

## The columns, and which defect each one answers

- `workspace_id` — PRIMARY KEY, and this is the exclusion that matters. One workspace can
  hold at most one reservation, enforced by the database rather than by the advisory lock
  alone. Kept as text, not `uuid`: `target.py` takes the workspace id from the trusted
  `OperationBinding`'s resolved principal and requires only that it be a non-blank
  string, and a column narrower than the identity the code admits would fail at INSERT
  time for an id the gates accepted.
- `state` — `reserved` (claim taken, step 3) or `registered` (published, step 9),
  constrained to exactly those two. `registry.py` reads this column to distinguish a
  completed registration (an idempotent replay) from a live claim (a refusal), so a third
  value arriving from anywhere would put that decision into undefined territory.
- `attempt_token` — the F10 fence. NOT NULL and constrained non-blank, because the whole
  point of the token is that a row cannot be held by nobody: `finalize` and `release`
  require it and compare it against this column under the lock, and the SQL narrows on it
  so the check and the mutation are one statement. A nullable column here would make the
  unfenced case representable, and representable is eventually reachable.
- `org_id` — GENERATED ALWAYS from `identity_json`, STORED, NOT NULL. This is the tenant
  identity constraint, and it is generated rather than written so it cannot disagree with
  the identity the reservation was actually taken for. A separately-written tenant column
  is a column that can be wrong; this one is derived from the same bytes the store
  compares identities against. Its NOT NULL is load-bearing beyond documentation: a row
  whose `identity_json` carries no `org_id` — or is not JSON at all — is refused by the
  database at INSERT, so an unattributable reservation cannot exist.
- `created_at` — for operators diagnosing a stranded reservation. Not read by any code
  path, and deliberately not used to expire anything: age cannot distinguish a dead
  attempt from a slow one, which is why taking over an abandoned claim goes through
  `release_claim` and the durable state file instead. That release compares a one-way
  fingerprint of this row's `attempt_token`, digested in SQL so the token never leaves the
  database, which is why no additional column was needed for it (F13).

## What this migration does NOT do

It creates no foreign key to `organizations` or `workspaces`. Both would be wrong here.
The bootstrap reservation is taken BEFORE the workspace is usable and by a package that
deliberately does not import the domain's models — and a workspace bootstrapped by the
`workspace_bootstrap` CLI against a workspace row that does not yet exist would have its
pre-mutation claim refused by a constraint, which is precisely the claim whose absence
leaves a cluster half-mutated with nothing recording it.

`identity_json` is text holding JSON rather than `jsonb` because `registry.py` compares
it as a canonical `sort_keys=True` serialization; storing it as `jsonb` would let the
database reorder and re-render it, so the bytes read back would not be the bytes written
and the identity comparison would be against a value nothing produced. The generated
column casts to `jsonb` at write time, which gets the validation without the rewrite.
"""

import sqlalchemy as sa

from alembic import op

revision = "017_add_workspace_bootstrap_reservations"
down_revision = "016_add_organization_grants"
branch_labels = None
depends_on = None

_TABLE = "workspace_bootstrap_reservations"

# Kept as module constants so the names in `upgrade` and `downgrade` cannot drift apart:
# a downgrade that drops an index by a misspelled name fails at exactly the moment
# someone needs it to work.
_STATE_CHECK = "ck_workspace_bootstrap_reservations_state"
_WORKSPACE_CHECK = "ck_workspace_bootstrap_reservations_workspace_id"
_TOKEN_CHECK = "ck_workspace_bootstrap_reservations_attempt_token"
_ORG_INDEX = "ix_workspace_bootstrap_reservations_org_id"
_STATE_INDEX = "ix_workspace_bootstrap_reservations_state"


def upgrade():
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.String(255), primary_key=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("identity_json", sa.Text(), nullable=False),
        sa.Column("attempt_token", sa.String(128), nullable=False),
        sa.Column(
            "org_id",
            sa.Text(),
            sa.Computed("(identity_json::jsonb ->> 'org_id')", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Exactly the two states `registry.py` branches on. `registry.REGISTERED` and
        # `registry.RESERVED` are the literals; they are spelled out here rather than
        # imported because a migration must render the same DDL forever, independent of
        # what a later revision of the application package happens to define.
        sa.CheckConstraint(
            "state IN ('reserved', 'registered')",
            name=_STATE_CHECK,
        ),
        # Blank-but-present is the partial-record failure mode this package refuses in
        # code (`WorkspaceTarget` rejects every blank field); the database refuses it too,
        # because a reservation keyed on '   ' is a claim on nothing that still occupies
        # the primary key.
        #
        # The predicate is a POSIX character class and NOT `btrim(x, ' \\t\\n...')`.
        # Bare `btrim(x)` trims spaces only, while every blankness check in the Python —
        # `registry.py`'s token guards, `registration.py`'s field validation — uses
        # `str.strip()`, which trims ALL whitespace; left as the default, a tab-only
        # `attempt_token` would be refused by the code and accepted by the database. That
        # is the direction that matters: the column exists so a claim held by nobody is
        # UNREPRESENTABLE, and a row the code would never write is exactly the row no code
        # path can clean up. Spelling the trim set out as escapes was the obvious fix and
        # is wrong here — this DDL is also RENDERED (Alembic offline mode, which is how the
        # tests apply it), and a literal tab or newline inside the SQL string survives
        # rendering as a real control character that splits the statement.
        sa.CheckConstraint("workspace_id !~ '^[[:space:]]*$'", name=_WORKSPACE_CHECK),
        sa.CheckConstraint("attempt_token !~ '^[[:space:]]*$'", name=_TOKEN_CHECK),
    )
    # Tenant-scoped reads: "what is this org currently bootstrapping", and the query an
    # operator runs to find a stranded claim.
    op.create_index(_ORG_INDEX, _TABLE, ["org_id"])
    # `_DELETE_RESERVATION` and `_READ_REGISTRATION` both filter on state. Small table,
    # so this is about the read path staying selective as reservations accumulate rather
    # than about current row counts.
    op.create_index(_STATE_INDEX, _TABLE, ["state"])
    op.create_table(
        "workspace_bootstrap_authority",
        sa.Column("workspace_id", sa.String(255), primary_key=True),
        sa.Column("generation", sa.String(64), primary_key=True),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("cluster_arn", sa.Text(), nullable=False),
        sa.Column("claim", sa.String(64), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("progress_json", sa.Text(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("generation ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("claim ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("operation_id !~ '^[[:space:]]*$'"),
        sa.CheckConstraint("org_id !~ '^[[:space:]]*$'"),
        sa.CheckConstraint("jsonb_typeof(plan_json::jsonb) = 'object'"),
        sa.CheckConstraint("jsonb_typeof(progress_json::jsonb) = 'object'"),
    )


def downgrade():
    op.drop_table("workspace_bootstrap_authority")
    op.drop_index(_STATE_INDEX, table_name=_TABLE)
    op.drop_index(_ORG_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
