"""Retain installation denial and retry authority after local claims are removed."""

import sqlalchemy as sa

from alembic import op

revision = "071_installation_revocation"
down_revision = "070_magic_link_delivery_method"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "installation_revocations",
        sa.Column("installation_id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("authorized_user_ids", sa.JSON(), nullable=False),
        sa.Column("provider_uninstall_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("provider_revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cleanup_pending", sa.JSON(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Dropping denial would reactivate stale routing. An operator must explicitly
    # reconcile retained revocations before downgrading this security boundary.
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM installation_revocations WHERE restored_at IS NULL")):
        raise RuntimeError("Cannot discard active installation revocations")
    op.drop_table("installation_revocations")
