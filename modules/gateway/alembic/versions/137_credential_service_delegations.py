"""Store narrowly scoped service delegations for Product reads."""

import sqlalchemy as sa

from alembic import op

revision = "137_service_delegations"
down_revision = "136_tool_adapter_activation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "credential_service_delegations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("user_credentials.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("operations", sa.JSON(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("visibility", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(255)),
        sa.Column("attempt_id", sa.String(255)),
        sa.Column("lease_fence", sa.Integer()),
        sa.Column("binding_generation", sa.Integer()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_service_delegations_principal", "credential_service_delegations", ["service_id", "tenant_id", "product_id", "project_id"])


def downgrade():
    raise RuntimeError("Retain service delegation revocation evidence during rollback")
