"""Persist legacy provider admissions without treating them as usage receipts."""

import sqlalchemy as sa

from alembic import op

revision = "115_legacy_metering_cohort"
down_revision = "114_merge_metering_product_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "metering_legacy_admissions",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("request_id", sa.String(255), primary_key=True),
        sa.Column("authenticated_tenant_id", sa.String(255), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("disposition", sa.String(32), nullable=True),
        sa.Column("disposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('pending', 'settled', 'not_dispatched')", name="ck_metering_legacy_admission_state"),
        sa.CheckConstraint(
            "(state = 'not_dispatched') = (disposition IS NOT NULL AND disposed_at IS NOT NULL)",
            name="ck_metering_legacy_admission_disposition",
        ),
    )
    op.create_index("ix_metering_legacy_admission_auth", "metering_legacy_admissions", ["authenticated_tenant_id", "state"])


def downgrade():
    raise RuntimeError("Legacy cohort admissions and pending provider outcomes must survive rollback")
