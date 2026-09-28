"""Global and flow budget enforcement controls, with durable missing-usage markers."""

import sqlalchemy as sa

from alembic import op

revision = "065_budget_enforcement_controls"
down_revision = "064_orchestration_run_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "budget_enforcement_settings",
        sa.Column("scope_key", sa.String(512), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "budget_accounting_gaps",
        sa.Column("request_id", sa.String(255), primary_key=True),
        sa.Column("scope_key", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_budget_accounting_gaps_scope_key", "budget_accounting_gaps", ["scope_key"])


def downgrade():
    op.drop_table("budget_accounting_gaps")
    op.drop_table("budget_enforcement_settings")
