"""Product-owned source grants and explicit refresh configuration.

Revision ID: 017_product_source_refresh
Revises: 016_l5_contributions
"""

from alembic import op

revision = "017_product_source_refresh"
down_revision = "016_l5_contributions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE knowledge_assets
          ADD COLUMN product_source jsonb,
          ADD COLUMN refresh_enabled boolean,
          ADD COLUMN refresh_error text,
          ADD COLUMN last_refreshed_at timestamptz,
          ADD CONSTRAINT ck_knowledge_product_source CHECK (
            product_source IS NULL OR (
              product_id IS NOT NULL AND asset_type = 'repo'
              AND jsonb_typeof(product_source) = 'object'
              AND refresh_enabled IS NOT NULL
            )
          );
    """)
    op.execute("""CREATE UNIQUE INDEX uq_knowledge_product_source
        ON knowledge_assets (tenant_id, product_id, (product_source->>'binding_id'))
        WHERE product_source IS NOT NULL AND status != 'removed';
    """)


def downgrade():
    # Dropping a grant must never restore the old, implicit refresh authority.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM knowledge_assets WHERE product_source IS NOT NULL) THEN
            RAISE EXCEPTION 'Retain Product source grants when rolling back application code';
        END IF;
    END $$""")
    op.drop_index("uq_knowledge_product_source", table_name="knowledge_assets")
    op.execute("""ALTER TABLE knowledge_assets
        DROP CONSTRAINT ck_knowledge_product_source,
        DROP COLUMN product_source, DROP COLUMN refresh_enabled,
        DROP COLUMN refresh_error, DROP COLUMN last_refreshed_at;
    """)
