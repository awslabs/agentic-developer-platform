"""Convert cluster state columns from Text to JSONB for efficient querying.

Revision ID: 004_jsonb_state_columns
Revises: 003_add_research_findings
Create Date: 2026-04-02

Converts:
  - clusters.actual_state_json: Text -> JSONB
  - clusters.desired_state_json: Text -> JSONB

This enables the Ingest Lambda to query workspace queue URLs from Aurora
Data API with native JSON operations, and supports future JSONB-based
filtering and indexing on cluster state data.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "004_jsonb_state_columns"
down_revision = "003_add_research_findings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "clusters",
        "actual_state_json",
        type_=JSONB,
        postgresql_using="actual_state_json::jsonb",
    )
    op.alter_column(
        "clusters",
        "desired_state_json",
        type_=JSONB,
        postgresql_using="desired_state_json::jsonb",
    )


def downgrade() -> None:
    op.alter_column(
        "clusters",
        "actual_state_json",
        type_=sa.Text,
        postgresql_using="actual_state_json::text",
    )
    op.alter_column(
        "clusters",
        "desired_state_json",
        type_=sa.Text,
        postgresql_using="desired_state_json::text",
    )
