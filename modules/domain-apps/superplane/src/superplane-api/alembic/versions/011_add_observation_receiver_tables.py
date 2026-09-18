"""Add observation receiver tables (receipts and leases).

Revision ID: 011_add_observation_receiver_tables
Revises: 010_add_workspace_grants
Create Date: 2026-09-18

Issue #5056 (U15), EPIC #4910. Additive only: two new tables, no column added to
or removed from an existing one, and no data migration. Nothing already in the
database changes meaning, so this can be applied before the monitor is cut over
and rolled back afterwards without touching domain data.

## Two notes for whoever runs this

U13 (#5045) repaired the historical duplicate and dangling revisions, and U14
(#5055) now extends that line through ``010_add_workspace_grants``. This
unapplied additive migration is therefore renumbered during integration and
extends that single head directly.

**It has not been applied anywhere.** U15 authors the schema; U23 (#5327) owns
deployment. `releases/superplane.lock.yaml` still records the chain as
``status: unverified`` because the real-database rehearsal remains gated; the
offline graph itself is single-headed and fully checked.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "011_add_observation_receiver_tables"
down_revision = "010_add_workspace_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "observation_receipts",
        sa.Column("cluster_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace", sa.String(255), nullable=False),
        sa.Column("submitter_id", sa.String(255), nullable=False),
        sa.Column("last_reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_body_sha256", sa.String(64), nullable=False),
        sa.Column("last_status", sa.String(32), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "observation_leases",
        sa.Column("scope", sa.String(255), primary_key=True),
        sa.Column("holder", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # BigInteger: monotonic, never reset, incremented once per acquire.
        sa.Column(
            "fence_token",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_holder", sa.String(255), nullable=True),
        sa.Column("acquire_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Supports the scoped read path, which filters recorded observations by the
    # workspace the caller is authorized for.
    op.create_index(
        "ix_observation_receipts_workspace", "observation_receipts", ["workspace"]
    )


def downgrade() -> None:
    op.drop_index("ix_observation_receipts_workspace", table_name="observation_receipts")
    op.drop_table("observation_leases")
    op.drop_table("observation_receipts")
