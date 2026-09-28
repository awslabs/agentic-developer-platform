"""Explicit ADP organization binding for fresh installations (U23)."""

import sqlalchemy as sa

from alembic import op

revision = "015_add_adp_org_binding"
down_revision = "014_add_provider_connections"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "organizations", sa.Column("adp_org_id", sa.String(255), nullable=True)
    )
    op.create_index(
        "ix_organizations_adp_org_id", "organizations", ["adp_org_id"], unique=True
    )


def downgrade():
    op.drop_index("ix_organizations_adp_org_id", table_name="organizations")
    op.drop_column("organizations", "adp_org_id")
