"""Accommodate Task harness contract names in probe slots and evidence."""

import sqlalchemy as sa

from alembic import op

revision = "083_probe_contract_revision"
down_revision = "082_aws_connection_trust"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("model_probe_slots", "model_invocability_evidence"):
        op.alter_column(table, "harness_contract_revision", existing_type=sa.String(32), type_=sa.String(64), existing_nullable=False)


def downgrade():
    # PostgreSQL rejects narrowing if a live Task contract would be truncated.
    for table in ("model_probe_slots", "model_invocability_evidence"):
        op.alter_column(table, "harness_contract_revision", existing_type=sa.String(64), type_=sa.String(32), existing_nullable=False)
