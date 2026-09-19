"""Story-to-PR bindings: the durable record of which PR implements which story.

Issue #5301 (EPIC #4191). Additive — creates one table, alters nothing, backfills
nothing.

**Why there is no backfill.** It is tempting to reconcile the existing
`awaiting_merge` stories by searching each one's repository for a merged PR that
mentions its issue. That search is exactly the authority this story refuses: a
title or body mention is a discovery hint, not evidence that a given PR delivered a
given story, and a migration that adopted candidates on that basis would assert
associations nobody verified — including reviewer-artifact PRs and unrelated
follow-ups. Historical unbound work is recovered through the attributed,
human-authorized path in `pr_bindings.recover_binding`, which records who
established the binding and against which verified head. A row this migration
invented would be indistinguishable from one a delivering run registered.

Must stay in agreement with `OrchestrationPullRequestBinding` in
`src/orchestration/models.py` — both are hand-written, so drift is the live risk:
the migration is what runs in dev, the models are what the tests use.
`tests/migrations/test_051_orchestration_pr_bindings.py` asserts that parity.

`provider_repository_id` and `installation_id` are `BigInteger` deliberately. They
hold GitHub's 64-bit provider integers; `Integer` would cap a value the provider is
free to grow, and the migration would still apply cleanly — the failure would
surface later as an overflow on a real id.

The revision id is shortened from the filename stem to stay inside the
`alembic_version.version_num` VARCHAR(32) limit that
`tests/migrations/test_revision_id_length.py` enforces.
"""

import sqlalchemy as sa

from alembic import op

revision = "051_orch_pr_bindings"
down_revision = "050_orchestration_work_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orchestration_pr_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("flow_id", sa.String(36), sa.ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("orchestration_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(255), nullable=False),
        sa.Column("provider_repository_id", sa.BigInteger(), nullable=False),
        sa.Column("provider_pr_node_id", sa.String(255), nullable=False),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("accepted_scope", sa.Text(), nullable=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("registered_by", sa.String(255), nullable=False),
        sa.Column("registered_by_kind", sa.String(16), nullable=False),
        sa.Column("recovery_reason", sa.Text(), nullable=True),
        sa.Column("superseded_reason", sa.String(255), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # THE idempotency invariant, enforced by the database rather than by the
    # registration code. Two concurrent registrations of the same pull request can
    # both pass an application-level "is this already bound?" read; only one can win
    # a unique index. Without this, a duplicated webhook, a retried request or a
    # restarted tick could each insert a binding, and reconciliation would then have
    # several candidate PRs for one story with no basis to choose.
    op.create_index(
        "uq_orchestration_pr_bindings_pr",
        "orchestration_pr_bindings",
        ["org_id", "provider_repository_id", "provider_pr_node_id"],
        unique=True,
    )
    # `TenantMixin` declares `org_id` with `index=True`, so the model implies this
    # standalone index; it is created explicitly here to keep migration/model parity
    # exact rather than leaving it to be inferred.
    op.create_index(
        "ix_orchestration_pr_bindings_org_id",
        "orchestration_pr_bindings",
        ["org_id"],
    )
    op.create_index(
        "ix_orchestration_pr_bindings_flow_id",
        "orchestration_pr_bindings",
        ["flow_id"],
    )
    # Reconciliation's hot read: the active binding for one node.
    op.create_index(
        "ix_orchestration_pr_bindings_node_id",
        "orchestration_pr_bindings",
        ["org_id", "node_id", "state"],
    )


def downgrade() -> None:
    # Unguarded, like 050's and for the same reason: binding rows are execution
    # bookkeeping, not a record of what a human approved, and this downgrade is the
    # documented rollback for the story. A raise here would block the very path the
    # rollback plan prescribes. Dropping the table cannot orphan anything — nothing
    # references it by foreign key.
    #
    # What a downgrade DOES cost is worth stating: stories whose completion evidence
    # was a binding revert to the issue-closure path, so any that relied on a
    # binding return to waiting rather than silently completing. That is the safe
    # direction, and it is why recovery is attributed and repeatable.
    op.drop_index("ix_orchestration_pr_bindings_node_id", table_name="orchestration_pr_bindings")
    op.drop_index("ix_orchestration_pr_bindings_flow_id", table_name="orchestration_pr_bindings")
    op.drop_index("ix_orchestration_pr_bindings_org_id", table_name="orchestration_pr_bindings")
    op.drop_index("uq_orchestration_pr_bindings_pr", table_name="orchestration_pr_bindings")
    op.drop_table("orchestration_pr_bindings")
