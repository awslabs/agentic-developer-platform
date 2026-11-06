"""Product storage settings and version-pinned document upload receipts.

Revision ID: 018_product_context_storage
Revises: 017_product_source_refresh
"""

from alembic import op

revision = "018_product_context_storage"
down_revision = "017_product_source_refresh"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE product_context_storage (
          tenant_id text NOT NULL, product_id text NOT NULL,
          revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
          encryption text NOT NULL CHECK (encryption IN ('shared','managed','customer')),
          kms_key_arn text NOT NULL,
          read_key_arns jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(read_key_arns)='array'),
          updated_by text NOT NULL, updated_at timestamptz NOT NULL DEFAULT NOW(),
          PRIMARY KEY (tenant_id, product_id)
        );""")
    op.execute("""CREATE TABLE product_context_uploads (
          id uuid PRIMARY KEY, tenant_id text NOT NULL, product_id text NOT NULL,
          bucket text NOT NULL, object_key text NOT NULL,
          filename text NOT NULL, content_type text NOT NULL,
          content_length bigint NOT NULL CHECK (content_length > 0),
          sha256 text NOT NULL CHECK (sha256 ~ '^[a-f0-9]{64}$'),
          kms_key_arn text NOT NULL, requested_by text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT NOW(), expires_at timestamptz NOT NULL,
          source_version_id text, asset_id uuid UNIQUE REFERENCES knowledge_assets(id),
          UNIQUE (bucket, object_key)
        );""")
    op.execute("""CREATE INDEX ix_context_uploads_scope ON product_context_uploads (tenant_id, product_id, created_at);""")
    op.execute("""ALTER TABLE knowledge_assets ADD COLUMN source_upload_id uuid REFERENCES product_context_uploads(id),
          ADD COLUMN source_object_version text,
          ADD CONSTRAINT ck_context_upload_owner CHECK (source_upload_id IS NULL OR
            (product_id IS NOT NULL AND asset_type='doc' AND source_object_version IS NOT NULL));""")
    op.execute("""CREATE UNIQUE INDEX uq_context_asset_upload ON knowledge_assets(source_upload_id) WHERE source_upload_id IS NOT NULL;
    """)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM product_context_uploads) OR EXISTS (SELECT 1 FROM product_context_storage) THEN
            RAISE EXCEPTION 'Retain Product storage settings, encryption history and upload receipts';
        END IF;
    END $$""")
    op.execute("""ALTER TABLE knowledge_assets DROP CONSTRAINT ck_context_upload_owner,
        DROP COLUMN source_upload_id, DROP COLUMN source_object_version;""")
    op.execute("DROP TABLE product_context_uploads")
    op.execute("DROP TABLE product_context_storage")
