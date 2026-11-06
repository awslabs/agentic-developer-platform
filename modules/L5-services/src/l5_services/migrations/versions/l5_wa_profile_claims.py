"""Retain exclusive native Profile identity claims across unlink.

Revision ID: l5_wa_profile_claims
Revises: l5_publication_prevention_merge
"""

from alembic import op
import sqlalchemy as sa
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_wa_profile_claims"
down_revision = "l5_publication_prevention_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_service_profile_claims",
        sa.Column("service_id", sa.String(32), primary_key=True),
        sa.Column("native_identity_key", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
    )


def downgrade():
    require_empty("l5_service_profile_claims")
    op.drop_table("l5_service_profile_claims")
