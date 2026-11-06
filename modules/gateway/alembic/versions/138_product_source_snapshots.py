"""Retain immutable Product-owned source receipts across admission and revocation."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "138_product_source_snapshots"
down_revision = "137_service_delegations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "product_source_snapshots",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("delegation_id", sa.String(36), nullable=False),
        sa.Column("delegation_revision", sa.BigInteger(), nullable=False),
        sa.Column("source_ref", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("receipt", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("retain_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "delegation_id", "idempotency_key"),
    )
    op.create_index("ix_product_source_snapshots_expiry", "product_source_snapshots", ["expires_at"])
    op.create_index("ix_product_source_snapshots_scope", "product_source_snapshots", ["tenant_id", "product_id", "project_id"])


def downgrade():
    raise RuntimeError("Retain Product source receipts through rollback and admission drain")
