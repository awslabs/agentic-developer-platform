"""Persist metadata-only fingerprints for idempotent vault writes."""

import sqlalchemy as sa

from alembic import op

revision = "066_vault_operation_fingerprint"
down_revision = "066_cred_evidence_delegation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "user_credentials",
        sa.Column("operation_fingerprint", sa.String(64), nullable=True),
    )


def downgrade():
    op.drop_column("user_credentials", "operation_fingerprint")
