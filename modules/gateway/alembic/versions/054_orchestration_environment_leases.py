"""Environment leases: one row per physical deployment target, globally.

Issue #5150 (ENGINE-D1, parent #5131). Additive — creates one table, alters
nothing, backfills nothing.

**No backfill, deliberately.** A lease row asserts "this physical deployment target
is currently held by this action under this ownership generation". For any deploy
already in flight at deploy time there is nothing to reconstruct that from: the
platform has never recorded a physical-target identity, so a synthesized row would
either claim a hold nobody took or declare a target free that something is actively
deploying to. Both are worse than an empty table, because both would be *trusted*.

**Dormant until its consumers exist.** Nothing dispatches a deployment today —
#5151 dispatches, #5152 verifies and rolls back. A schema-only deploy therefore
changes no runtime behavior, which is what makes it safe to land ahead of them.

**Migration numbering.** Chained onto `053_flow_slug_unique`, the single head of the
live chain. The number comes from the chain, not from any number in the issue text.

This revision was authored as `053` against `052_orchestration_executions`, which
was the head at the time. `053_flow_slug_unique` (#5342/#4898) landed on `main`
first and took the same parent, leaving two children of one parent — a branched
history, which makes `alembic upgrade head` fail outright with "Multiple head
revisions are present". Renumbering to `054` and chaining onto that revision
restores a single linear chain.

Reordering is safe here because the two migrations are disjoint: `053` adds a
unique index to the existing `orchestration_flows`, while this one *creates* a new
table and touches nothing that already exists. Sequencing after `053` is therefore
semantically identical to sequencing before it.

Merges touching the chain must be serialized and the single-head check re-run;
`tests/migrations/test_054_orchestration_environment_leases.py` asserts exactly one
head, so the next lane to collide with this one fails in CI rather than in a
deployed database.

Must stay in agreement with `OrchestrationEnvironmentLease` in
`src/orchestration/models.py`. Both are hand-written, so drift is the live risk and
it fails in the worst possible place: the migration is what runs in the deployed
database while the models are what the tests use, so a mismatch passes every test
and raises `UndefinedColumn` in dev. The parity assertions in that test file catch
it.

WHY THE UNIQUE INDEX IS NOT TENANT-SCOPED
-----------------------------------------
`uq_orchestration_environment_leases_target` is on `canonical_target_key` **alone**.
Every other unique index in this chain leads with `org_id`, so this one looks like
an omission and will invite a "fix" — it is not one, and adding `org_id` would
reintroduce the exact defect the table exists to prevent.

One AWS account can be connected to the platform twice, under two connection ids,
possibly in two different tenants, both pointing at one real EKS cluster. A
connection id is an *alias*; the cluster is the *physical target*. Uniqueness has to
hold across tenants, or two tenants' aliases for one cluster could both be held at
once and two incompatible releases would be deployed on top of each other. Tenant
isolation is preserved in the *answer* the store gives — a contention refusal names
no holder, no tenant and no account — not in the index.

There is therefore also no `ix_..._org_id`: the table does not inherit `TenantMixin`
and `owner_org_id` is an ordinary nullable column recording who currently holds the
target, not a scoping key.
"""

import sqlalchemy as sa

from alembic import op

revision = "054_orch_environment_leases"
down_revision = "053_flow_slug_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orchestration_environment_leases",
        sa.Column("id", sa.String(36), primary_key=True),
        # The opaque canonical identity of one physical deployment surface: a
        # version-tagged digest over the normalized provider, account, region,
        # resource kind and resource id, derived in
        # src/orchestration/deployment_manifest.py. Opaque on purpose — an operator
        # reads the surface from evidence_* below, while the key itself discloses
        # nothing about another tenant's infrastructure if it is ever logged.
        sa.Column("canonical_target_key", sa.String(128), nullable=False),
        # Where the identity came from and when it was proven. NOT NULL because an
        # unevidenced canonicalization is a guess, and a guess is what decides
        # whether two aliases are the same cluster.
        sa.Column("evidence_source", sa.String(255), nullable=False),
        sa.Column("evidence_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_detail", sa.Text(), nullable=True),
        # LeaseState vocabulary in src/orchestration/environment_leases.py: held or
        # free. A string column, not a native enum, so a new member needs no DDL —
        # the same choice every neighbouring orchestration table makes.
        sa.Column("state", sa.String(16), nullable=False),
        # Who holds it. Nullable because the row outlives the hold: a released lease
        # keeps its canonicalization evidence so an operator can still ask why two
        # aliases were treated as one target.
        sa.Column("owner_org_id", sa.String(255), nullable=True),
        sa.Column("owner_action_id", sa.String(255), nullable=True),
        # The ownership fence, monotonic and never lowered. This is what stops a
        # superseded actor releasing the lease its successor now holds.
        sa.Column("owner_generation", sa.Integer(), nullable=False),
        # Which reviewed manifest entry authorized the hold, so a live deployment
        # can be traced back to the approval that permitted it.
        sa.Column("manifest_entry_id", sa.String(255), nullable=True),
        sa.Column("release_ref", sa.String(255), nullable=True),
        # The compare-and-set fence, advancing by exactly one per applied write. A
        # caller presenting a revision the row has passed is stale by construction,
        # which is what stops a lost update.
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        # Evidence of lost contact only, never of an exit. A deployment pipeline
        # partitioned from us is still rolling pods, so expiry alone never licenses
        # takeover — that needs reconciled_terminal_evidence below.
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        # What a process that actually looked observed about the holder's
        # deployment. The ONLY thing that unblocks taking over a lapsed target.
        sa.Column("reconciled_terminal_evidence", sa.Text(), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(64), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # THE invariant of this story, and note what is absent: org_id. See the module
    # docstring — scoping this by tenant is the defect, not the fix. Two concurrent
    # acquisitions can both pass an application-level "is this target free?" read;
    # only a unique index refuses the second, which is what makes the lease binding
    # rather than advisory. It is also the sole backstop on SQLite, where
    # SELECT ... FOR UPDATE is a no-op.
    op.create_index(
        "uq_orchestration_environment_leases_target",
        "orchestration_environment_leases",
        ["canonical_target_key"],
        unique=True,
    )
    # An operator's read path: what is this tenant currently holding?
    op.create_index(
        "ix_orchestration_environment_leases_owner",
        "orchestration_environment_leases",
        ["owner_org_id", "state"],
    )
    # The reconciliation sweep's read path: held leases whose contact window has
    # lapsed and which therefore need somebody to go and look.
    op.create_index(
        "ix_orchestration_environment_leases_expiry",
        "orchestration_environment_leases",
        ["state", "lease_expires_at"],
    )


def downgrade() -> None:
    """Drop the table.

    Unguarded, like 050's and 052's, and this IS the documented rollback for the
    story — a `raise` here would block the very path the rollback plan prescribes
    (stop admitting deploys, reconcile any held target, then downgrade). What must
    survive a rollback is the record of what a human approved, and that lives in
    `src/orchestration/manifests/orchestration-deployments.yaml` under review, not
    in these rows.

    One precondition, because it is not recoverable afterwards: reconcile any
    `state = 'held'` row first. Dropping the table while a deployment is in flight
    discards the only record that the target is held, so the next deploy after a
    re-upgrade would find it free and start a second one. Dropping an all-free
    table is safe.

    Nothing references this table by foreign key, so the drop cannot orphan
    anything.

    Note for validators: `upgrade/downgrade/upgrade` is a check to run against a
    disposable test database. A shared environment is never downgraded as a
    validation step.
    """
    op.drop_index(
        "ix_orchestration_environment_leases_expiry",
        table_name="orchestration_environment_leases",
    )
    op.drop_index(
        "ix_orchestration_environment_leases_owner",
        table_name="orchestration_environment_leases",
    )
    op.drop_index(
        "uq_orchestration_environment_leases_target",
        table_name="orchestration_environment_leases",
    )
    op.drop_table("orchestration_environment_leases")
