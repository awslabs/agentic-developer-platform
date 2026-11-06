"""Explicit organization-owned Atlassian service-account bindings."""

import sqlalchemy as sa

from alembic import op

revision = "169_atlassian_system"
down_revision = "168_merge_security_wa"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "atlassian_system_connections",
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("user_credentials.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("revision", sa.String(36), nullable=False),
        sa.Column("provider_account_id", sa.String(255)),
        sa.Column("site_id", sa.String(36), nullable=False),
        sa.Column("workload_name", sa.String(128), nullable=False),
        sa.Column("provider_name", sa.String(128), nullable=False),
        sa.Column("allowed_product_ids", sa.JSON, nullable=False),
        sa.Column("schemas", sa.JSON, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_atlassian_system_connections_org_id", "atlassian_system_connections", ["org_id"])
    op.create_table(
        "atlassian_system_keys",
        sa.Column("provider_name", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("user_credentials.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_atlassian_system_keys_org_id", "atlassian_system_keys", ["org_id"])
    op.create_index("ix_atlassian_system_keys_credential_id", "atlassian_system_keys", ["credential_id"])


def downgrade():
    raise RuntimeError("Retain Jira Identity provider references during application rollback; revoke connections through the managed API.")
