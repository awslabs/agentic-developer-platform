"""Independent organization authority for zero-workspace bootstrap (#5535)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "016_add_organization_grants"
down_revision = "015_add_adp_org_binding"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "organization_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("principal", sa.String(255), nullable=False),
        sa.Column("principal_type", sa.String(32), nullable=False),
        sa.Column("permissions", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.String(255), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "org_id", "principal", name="uq_organization_grants_principal"
        ),
    )
    op.create_index("ix_organization_grants_org_id", "organization_grants", ["org_id"])


def downgrade():
    op.drop_index("ix_organization_grants_org_id", table_name="organization_grants")
    op.drop_table("organization_grants")
