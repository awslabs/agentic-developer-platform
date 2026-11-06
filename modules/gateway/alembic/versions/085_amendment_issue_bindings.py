"""Durable amendment work issue bindings (#5329).

Additive: one new table, nothing altered or backfilled. Publication of new amendment
work used to reconcile against a provider listing bounded by a time window, which could
miss an existing issue (busy repository, changed document) and create a duplicate, and
refused permanently when the issue's title or body drifted. This table makes the
database the authority: `(org_id, repository, proposal_identity)` is unique, the row is
committed `pending` before the provider create call and completed `created` after it.

Must stay in agreement with `OrchestrationAmendmentIssueBinding` in
`src/orchestration/models.py`.
"""

import sqlalchemy as sa

from alembic import op

revision = "085_amendment_issue_bindings"
down_revision = "084_reservation_receipt_scopes"
branch_labels = None
depends_on = None

TABLE = "orchestration_amendment_issue_bindings"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("proposal_identity", sa.String(255), nullable=False),
        sa.Column("provider_repository_id", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("claim_token", sa.String(36), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column("issue_node_id", sa.String(255), nullable=True),
        sa.Column("issue_url", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The idempotency invariant: one issue per tenant, repository and proposed work.
    op.create_index(
        "uq_orchestration_amendment_issue_bindings_identity",
        TABLE,
        ["org_id", "repository", "proposal_identity"],
        unique=True,
    )
    # `TenantMixin` declares `org_id` with `index=True`; explicit for model parity.
    op.create_index("ix_orchestration_amendment_issue_bindings_org_id", TABLE, ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_orchestration_amendment_issue_bindings_org_id", table_name=TABLE)
    op.drop_index("uq_orchestration_amendment_issue_bindings_identity", table_name=TABLE)
    op.drop_table(TABLE)
