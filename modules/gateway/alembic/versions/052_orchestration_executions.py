"""Delivery ledger: execution and action records with transactional transitions.

Issue #5142 (ENGINE-K1, parent #5122). Additive — creates two tables, alters
nothing, backfills nothing.

**No backfill, deliberately.** An execution row asserts "this node's delivery work
is being carried out under this authority", including which accepted plan version
and which ownership claim generation admitted it. None of that is recoverable for
work already in flight at deploy time: inventing rows would assert an authority
nobody verified, and inventing a phase would assert progress nobody observed. Work
in flight is reconciled by its own run, not by this migration.

**Dormant until its consumers exist.** Nothing reads these tables yet — the runner
(#5143) and the read model (#5145) are separate issues. A schema-only deploy
therefore changes no runtime behavior, which is what makes it safe to land before
them rather than alongside them.

**Migration numbering.** Chained onto `051_orch_pr_bindings`, which is the real
single head of the live chain at authoring time (`044_ratelimit_org_type` and
`045_pricing_seed_2026_09_12_1` are not heads — `046_merge_pricing_ratelimit`
merges both via a tuple `down_revision`). The number comes from the chain, not from
any number in the issue text. Merges touching the chain must be serialized and the
single-head check re-run; `tests/migrations/test_052_orchestration_executions.py`
asserts it.

Must stay in agreement with `OrchestrationExecution` / `OrchestrationAction` in
`src/orchestration/models.py`. Both are hand-written, so drift is the live risk and
it fails in the worst place: the migration is what runs in the deployed database,
the models are what the tests use, so a mismatch passes every test and raises
`UndefinedColumn` in dev. The parity assertions in that test file are what catch
it.

`detail` is declared with the same dialect variant as `JSON_DOC` in
`models.py` and `029_orchestration_graph.py` — real `JSONB` on PostgreSQL, plain
`JSON` on the SQLite the tests run against. A bare `JSON()` would render as `JSON`
on PostgreSQL too and quietly forfeit JSONB.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "052_orchestration_executions"
down_revision = "051_orch_pr_bindings"
branch_labels = None
depends_on = None

# Must stay identical to JSON_DOC in src/orchestration/models.py.
JSON_DOC = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "orchestration_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        # Tenant-safe foreign keys: both parents carry org_id, and every store
        # query filters org_id alongside the id, so a cross-tenant id cannot
        # resolve to a readable row.
        sa.Column("flow_id", sa.String(36), sa.ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        # Phase/status vocabulary lives in src/orchestration/execution_state.py.
        # String columns, not native enums, so a new member needs no DDL — the same
        # choice the existing orchestration tables make.
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        # The compare-and-set fence. A caller presenting a revision the row has
        # passed is stale by construction, which is what stops a lost update.
        sa.Column("revision", sa.Integer(), nullable=False),
        # Authority this execution was admitted under, re-verified inside each
        # writing transaction. 0 is a legal accepted_plan_version: policy admission
        # reports 0 when no accepted plan exists (the legacy path).
        sa.Column("accepted_plan_version", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.String(36), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        # Written in the same transaction as phase/status. A non-terminal row with
        # no next check time is work no runner will ever pick up again.
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("progress_note", sa.Text(), nullable=True),
        # A block names its code, owner and required input so an operator can route
        # it from this row alone, without logs that expire.
        sa.Column("block_code", sa.String(64), nullable=True),
        sa.Column("block_owner", sa.String(255), nullable=True),
        sa.Column("block_required_input", sa.Text(), nullable=True),
        sa.Column("block_remaining_gates", sa.Text(), nullable=True),
        sa.Column("block_detail", sa.Text(), nullable=True),
        # Sanitized references only — an S3 key, a PR node id, a receipt id. Never a
        # credential and never a complete transcript: these rows are read by
        # operators, so a secret here would be a disclosure with no revocation.
        sa.Column("pending_action_key", sa.String(255), nullable=True),
        sa.Column("notification_receipt_ref", sa.String(255), nullable=True),
        sa.Column("handoff_receipt_ref", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # THE identity invariant. Two concurrent starts can both pass an
    # application-level "is there one already?" read; only a unique index refuses
    # the second. Without this the ledger would be advisory.
    op.create_index(
        "uq_orchestration_executions_cycle",
        "orchestration_executions",
        ["org_id", "node_id", "cycle"],
        unique=True,
    )
    # TenantMixin declares org_id with index=True; the migration must create it or
    # the models and the deployed schema disagree.
    op.create_index("ix_orchestration_executions_org_id", "orchestration_executions", ["org_id"])
    # The due-work read path a runner (#5143) uses on every pass. Status first
    # because it is the more selective predicate once concluded rows accumulate.
    op.create_index(
        "ix_orchestration_executions_due",
        "orchestration_executions",
        ["org_id", "status", "next_check_at"],
    )
    op.create_index(
        "ix_orchestration_executions_flow_id",
        "orchestration_executions",
        ["org_id", "flow_id"],
    )

    op.create_table(
        "orchestration_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column(
            "execution_id",
            sa.String(36),
            sa.ForeignKey("orchestration_executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The caller-supplied idempotency key, derived from the work rather than
        # generated per attempt. See OrchestrationAction's docstring.
        sa.Column("operation_key", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("artifact_ref", sa.String(512), nullable=True),
        sa.Column("receipt_ref", sa.String(512), nullable=True),
        sa.Column("detail", JSON_DOC, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # NULL while prepared/dispatched, and NULL for an action left `unknown`:
        # the absence of an observation time is itself the record that nobody
        # managed to look.
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # THE idempotency invariant: one action per operation key per execution per
    # tenant. This is what makes crash-and-retry safe — a repeated preparation
    # returns the original record instead of opening a second pull request.
    op.create_index(
        "uq_orchestration_actions_operation",
        "orchestration_actions",
        ["org_id", "execution_id", "operation_key"],
        unique=True,
    )
    op.create_index("ix_orchestration_actions_org_id", "orchestration_actions", ["org_id"])
    # Recovery's read path: the unresolved actions of one execution.
    op.create_index(
        "ix_orchestration_actions_execution_status",
        "orchestration_actions",
        ["org_id", "execution_id", "status"],
    )


def downgrade() -> None:
    """Drop both tables, child first.

    Unguarded, like 050's. These rows are execution bookkeeping rather than a
    record of what a human approved, and this IS the documented rollback for the
    story — a `raise` here would block the very path the rollback plan prescribes
    ("disable new admissions, reconcile pending effects, then downgrade"). The
    evidence a rollback must retain is the shared ledger and inventory this
    migration does not touch.

    Actions are dropped before executions because the FK points that way; the
    reverse order fails on PostgreSQL. Nothing else references either table, so
    this cannot orphan anything.

    Note for validators: `upgrade/downgrade/upgrade` is a check to run against a
    disposable test database. A shared environment is never downgraded as a
    validation step.
    """
    op.drop_index("ix_orchestration_actions_execution_status", table_name="orchestration_actions")
    op.drop_index("ix_orchestration_actions_org_id", table_name="orchestration_actions")
    op.drop_index("uq_orchestration_actions_operation", table_name="orchestration_actions")
    op.drop_table("orchestration_actions")

    op.drop_index("ix_orchestration_executions_flow_id", table_name="orchestration_executions")
    op.drop_index("ix_orchestration_executions_due", table_name="orchestration_executions")
    op.drop_index("ix_orchestration_executions_org_id", table_name="orchestration_executions")
    op.drop_index("uq_orchestration_executions_cycle", table_name="orchestration_executions")
    op.drop_table("orchestration_executions")
