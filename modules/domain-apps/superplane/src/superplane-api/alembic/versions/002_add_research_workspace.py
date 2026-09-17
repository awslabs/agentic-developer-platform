"""Add research workspace support — budget guardrails + agent IAM role.

Revision ID: 002_add_research_workspace
Revises: 001_initial
Create Date: 2026-03-30

Adds:
  - workspaces.budget_max_daily_usd (NUMERIC(10,2)) — max daily spend guardrail
  - workspaces.budget_max_gpus (INTEGER) — max GPU count guardrail
  - workspaces.agent_iam_role_arn (VARCHAR(512)) — agent IAM role for research workspaces

The isolation_mode column already accepts free-form strings;
the application layer now validates 'dedicated', 'namespace', or 'research'.
"""

from alembic import op
import sqlalchemy as sa

revision = "002_add_research_workspace"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("budget_max_daily_usd", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("budget_max_gpus", sa.Integer, nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("agent_iam_role_arn", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "agent_iam_role_arn")
    op.drop_column("workspaces", "budget_max_gpus")
    op.drop_column("workspaces", "budget_max_daily_usd")
