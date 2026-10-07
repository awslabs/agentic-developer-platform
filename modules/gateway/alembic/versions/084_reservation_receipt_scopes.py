"""Retain proxy-verified reservation scope bindings for settlement recovery."""

import sqlalchemy as sa

from alembic import op

revision = "084_reservation_receipt_scopes"
down_revision = "083_probe_contract_revision"
branch_labels = None
depends_on = None


def upgrade():
    # Existing receipts intentionally remain NULL: pricing alone does not prove
    # that the provider's usage passed the reservation adapter's strict check.
    if "reservation_scope_keys" in {column["name"] for column in sa.inspect(op.get_bind()).get_columns("budget_settlement_receipts")}:
        return
    op.add_column("budget_settlement_receipts", sa.Column("reservation_scope_keys", sa.JSON(), nullable=True))


def downgrade():
    # Older code ignores this nullable column. Preserve recovery evidence while
    # allowing rollback through earlier additive migrations, as revision 079 does.
    pass
