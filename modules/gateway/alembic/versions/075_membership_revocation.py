"""Preserve tenant removal tombstones without deleting a global login."""

import sqlalchemy as sa

from alembic import op

revision = "075_membership_revocation"
down_revision = "074_budget_settlement_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tenant_memberships", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    # A rollback must not silently revive memberships. Retain the tombstones.
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM tenant_memberships WHERE revoked_at IS NOT NULL)")):
        raise RuntimeError("Membership revocation tombstones must survive rollback")
    op.drop_column("tenant_memberships", "revoked_at")
