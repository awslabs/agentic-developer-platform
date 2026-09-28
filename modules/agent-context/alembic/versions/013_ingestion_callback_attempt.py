"""Bind knowledge callbacks to the current server-dispatched attempt."""
from alembic import op

revision = "013_ingestion_callback_attempt"
down_revision = "012_partial_unique_excl_removed"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE knowledge_assets
        ADD COLUMN ingestion_attempt_id UUID,
        ADD COLUMN callback_grant_sha256 VARCHAR(64)""")


def downgrade():
    op.drop_column("knowledge_assets", "callback_grant_sha256")
    op.drop_column("knowledge_assets", "ingestion_attempt_id")
