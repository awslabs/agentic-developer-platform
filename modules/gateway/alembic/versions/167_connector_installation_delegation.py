"""Bind collection delegation directly to an existing installation or credential."""

import sqlalchemy as sa

from alembic import op

revision = "167_connector_installation"
down_revision = "166_l5_wa_generation"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("credential_service_delegations", "credential_id", existing_type=sa.String(36), nullable=True)
    op.add_column("credential_service_delegations", sa.Column("installation_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_service_delegation_installation", "credential_service_delegations", "channel_tenant_map", ["installation_id"], ["id"], ondelete="CASCADE"
    )
    op.create_check_constraint(
        "ck_service_delegation_source", "credential_service_delegations", "(credential_id IS NOT NULL) != (installation_id IS NOT NULL)"
    )


def downgrade():
    raise RuntimeError("Installation delegation retains work history and cannot safely be reversed")
