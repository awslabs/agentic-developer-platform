"""Record server-owned AWS connection verification provenance."""

import sqlalchemy as sa

from alembic import op

revision = "067_aws_connection_verify"
down_revision = "066_vault_operation_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_credentials",
        sa.Column("aws_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_credentials", "aws_verified_at")
