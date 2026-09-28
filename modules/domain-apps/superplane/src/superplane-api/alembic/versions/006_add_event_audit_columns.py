"""Add audit columns to events table.

Revision ID: 006_add_event_audit_columns
Revises: 006_add_workspace_reconciler_fields
Create Date: 2026-04-09

Adds user_id, action, source_ip, request_path, http_status columns
and performance indexes for audit event queries.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = "006_add_event_audit_columns"
down_revision = "006_add_workspace_reconciler_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add audit columns and indexes to events table."""
    # Add new columns
    op.add_column("events", sa.Column("user_id", sa.String(255), nullable=True))
    op.add_column(
        "events",
        sa.Column("action", sa.String(50), nullable=False, server_default="unknown"),
    )
    op.add_column("events", sa.Column("source_ip", sa.String(45), nullable=True))
    op.add_column("events", sa.Column("request_path", sa.String(512), nullable=True))
    op.add_column("events", sa.Column("http_status", sa.Integer(), nullable=True))

    # Make resource_id nullable (middleware may not always know the resource ID)
    op.alter_column(
        "events", "resource_id", existing_type=postgresql.UUID(), nullable=True
    )

    # Remove server default for action after backfill
    op.alter_column("events", "action", server_default=None)

    # Add performance indexes
    op.create_index("ix_events_org_id_created_at", "events", ["org_id", "created_at"])
    op.create_index("ix_events_resource_type", "events", ["resource_type"])
    op.create_index("ix_events_user_id", "events", ["user_id"])


def downgrade() -> None:
    """Remove audit columns and indexes from events table."""
    op.drop_index("ix_events_user_id", table_name="events")
    op.drop_index("ix_events_resource_type", table_name="events")
    op.drop_index("ix_events_org_id_created_at", table_name="events")

    op.alter_column(
        "events", "resource_id", existing_type=postgresql.UUID(), nullable=False
    )

    op.drop_column("events", "http_status")
    op.drop_column("events", "request_path")
    op.drop_column("events", "source_ip")
    op.drop_column("events", "action")
    op.drop_column("events", "user_id")
