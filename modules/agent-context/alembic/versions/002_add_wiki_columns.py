"""Add wiki_status and wiki_s3_key columns to repositories table.

Issue #1382: DeepWiki output → S3 (human browse) + S3 Vectors (semantic embeddings).
These columns track where the wiki markdown lives in S3, enabling the `browse` verb
and letting the ingestion pipeline record wiki generation status.

Revision ID: 002_add_wiki_columns
Revises: 001_knowledge_layer_schema
Create Date: 2026-06-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "002_add_wiki_columns"
down_revision: str = "001_knowledge_layer_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add wiki tracking columns to repositories table."""
    # wiki_status: tracks DeepWiki generation + S3 upload + embedding status
    op.execute("""
        ALTER TABLE repositories
        ADD COLUMN wiki_status VARCHAR(32) NOT NULL DEFAULT 'pending'
    """)

    # wiki_s3_key: S3 object key for the wiki markdown (nullable — set on success)
    op.execute("""
        ALTER TABLE repositories
        ADD COLUMN wiki_s3_key VARCHAR(1024)
    """)


def downgrade() -> None:
    """Remove wiki columns from repositories table."""
    op.execute("ALTER TABLE repositories DROP COLUMN wiki_s3_key")
    op.execute("ALTER TABLE repositories DROP COLUMN wiki_status")
