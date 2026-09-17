"""Add is_default and budget_max_hourly_usd to workspaces for US-06.

Revision ID: 006_add_default_workspace_fields
Revises: 005_add_research_proposals
Create Date: 2026-04-09

Adds:
  - is_default boolean column (marks the platform's own workspace, not deletable via CLI)
  - budget_max_hourly_usd decimal column (hourly spend guardrail)
  - Partial unique index on is_default to enforce at most one default workspace
"""

from alembic import op
import sqlalchemy as sa

revision = "006_add_default_workspace_fields"
down_revision = "005_add_research_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "budget_max_hourly_usd",
            sa.Numeric(10, 2),
            nullable=True,
        ),
    )

    # Enforce at most one default workspace per org
    op.create_index(
        "ix_workspaces_is_default_unique",
        "workspaces",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("is_default = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_workspaces_is_default_unique")
    op.drop_column("workspaces", "budget_max_hourly_usd")
    op.drop_column("workspaces", "is_default")
