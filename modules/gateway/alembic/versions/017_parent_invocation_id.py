"""Add parent_invocation_id column to action_provenance.

Issue #1460: Capture run-to-run parent edge for agent-to-agent lineage.
Adds a nullable parent_invocation_id (references the upstream run's
message_id/invocation_id) so lineage is a first-class queryable tree.

Nullable, no backfill — existing rows stay null (pre-feature runs have no parent edge).

Revision ID: 017_parent_invocation_id
Revises: 016_action_provenance
Create Date: 2026-06-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "017_parent_invocation_id"
down_revision: str | None = "016_action_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add parent_invocation_id nullable column to action_provenance."""
    op.execute("""
        ALTER TABLE action_provenance
        ADD COLUMN parent_invocation_id VARCHAR(255) NULL
    """)


def downgrade() -> None:
    """Remove parent_invocation_id column from action_provenance."""
    op.drop_column("action_provenance", "parent_invocation_id")
