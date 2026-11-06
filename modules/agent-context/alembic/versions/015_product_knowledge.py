"""Add Product Knowledge ownership without changing existing record ownership.

Revision ID: 015_product_knowledge
Revises: 014_verified_public_acl
"""

from alembic import op
import sqlalchemy as sa

revision = "015_product_knowledge"
down_revision = "014_verified_public_acl"
branch_labels = None
depends_on = None


def upgrade():
    # Products live in the Gateway database; existence/membership is enforced
    # there. This is not the legacy Knowledge project_id.
    op.add_column("knowledge_assets", sa.Column("product_id", sa.String(36), nullable=True))
    op.create_check_constraint(
        "ck_knowledge_assets_product_scope",
        "knowledge_assets",
        "product_id IS NULL OR (product_id <> '' AND tenant_id IS NOT NULL AND tenant_id <> '' AND owner_sub IS NULL AND project_id IS NULL)",
    )
    op.drop_index("uq_knowledge_assets_source_scope", table_name="knowledge_assets")
    op.execute("""CREATE UNIQUE INDEX uq_knowledge_assets_source_scope ON knowledge_assets
        (source_ref, COALESCE(tenant_id, ''), COALESCE(owner_sub, ''), COALESCE(product_id, ''))
        WHERE status != 'removed'""")
    op.create_index(
        "ix_knowledge_assets_product",
        "knowledge_assets",
        ["tenant_id", "product_id", "created_at"],
        postgresql_where=sa.text("product_id IS NOT NULL AND status != 'removed'"),
    )


def downgrade():
    # Dropping Product ownership would turn private rows into tenant-owned rows.
    # Refuse a destructive downgrade while any Product records exist.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM knowledge_assets WHERE product_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Cannot remove Product ownership while Product knowledge exists';
        END IF;
    END $$""")
    op.drop_index("ix_knowledge_assets_product", table_name="knowledge_assets")
    op.drop_index("uq_knowledge_assets_source_scope", table_name="knowledge_assets")
    op.drop_constraint("ck_knowledge_assets_product_scope", "knowledge_assets", type_="check")
    op.drop_column("knowledge_assets", "product_id")
    op.execute("""CREATE UNIQUE INDEX uq_knowledge_assets_source_scope ON knowledge_assets
        (source_ref, COALESCE(tenant_id, ''), COALESCE(owner_sub, '')) WHERE status != 'removed'""")
