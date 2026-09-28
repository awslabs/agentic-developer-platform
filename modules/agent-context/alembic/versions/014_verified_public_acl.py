"""Quarantine historical wildcard ACLs until public visibility is re-observed.

The old default ["*"] cannot distinguish a public repository from an unverified
private one. No existing row is promoted. Trusted ingestion/backfill can set the
marker only after authoritative public-source observation. Readers fail closed
if this column is unavailable; deploy this migration before the new reader.

Revision ID: 014_verified_public_acl
Revises: 013_ingestion_callback_attempt
"""

from alembic import op
import sqlalchemy as sa

revision = "014_verified_public_acl"
down_revision = "013_ingestion_callback_attempt"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "repositories",
        sa.Column("acl_public_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    # Older readers trust '*'. Remove only unverified wildcards before dropping
    # the marker so a schema/code rollback cannot reopen the legacy exposure.
    # Explicit private principals and verified public ACLs remain intact.
    op.execute("""UPDATE repositories
        SET allowed_principals = allowed_principals - '*'
        WHERE acl_public_verified IS NOT TRUE AND allowed_principals ? '*'""")
    op.drop_column("repositories", "acl_public_verified")
