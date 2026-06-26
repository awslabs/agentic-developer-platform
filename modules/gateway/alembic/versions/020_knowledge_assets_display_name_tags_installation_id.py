"""Add display_name, tags, installation_id to knowledge_assets.

Issue #2084 (#2082 Phase-1 story 1): The registry API routes (routes.py)
INSERT into display_name and tags columns that migration 019 never defined
(pre-existing bug from the #2045 registry relocation, architect caveat C1).
This migration adds the missing columns so the INSERT no longer raises
UndefinedColumn, and adds installation_id (BIGINT) for the ingestion worker
to mint per-installation GitHub tokens (architect caveat C3).

Revision ID: 020_knowledge_assets_display_name_tags_installation_id
Revises: 019_knowledge_assets
Create Date: 2026-06-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "020_knowledge_assets_display_name_tags_installation_id"
down_revision: str | None = "019_knowledge_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add display_name, tags, and installation_id columns."""
    op.execute("""
        ALTER TABLE knowledge_assets
            ADD COLUMN display_name VARCHAR(512),
            ADD COLUMN tags JSONB DEFAULT '{}'::jsonb,
            ADD COLUMN installation_id BIGINT
    """)


def downgrade() -> None:
    """Remove display_name, tags, and installation_id columns."""
    op.drop_column("knowledge_assets", "installation_id")
    op.drop_column("knowledge_assets", "tags")
    op.drop_column("knowledge_assets", "display_name")
