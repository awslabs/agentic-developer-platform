"""Store only hashes of reservation-scoped bootstrap observation credentials."""

from alembic import op
import sqlalchemy as sa

revision = "018_bootstrap_read_tokens"
down_revision = "017_add_workspace_bootstrap_reservations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_bootstrap_read_tokens",
        sa.Column("workspace_id", sa.String(255), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("operation_id", sa.String(255), nullable=False),
        sa.Column("registration_claim", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("lease_holder", sa.String(255), nullable=False),
        sa.Column("lease_attempt_id", sa.String(255), nullable=False),
        sa.Column("lease_fence_token", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("workspace_bootstrap_read_tokens")
