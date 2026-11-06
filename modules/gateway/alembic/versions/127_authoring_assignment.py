"""Store the approved authoring publication identity and envelope.

Revision ID: 127_authoring_assignment
Revises: 126_tracker_publication_outbox
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "127_authoring_assignment"
down_revision = "126_tracker_publication_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "orchestration_amendment_requests",
        sa.Column("authoring_assignment", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
    )


def downgrade():
    op.drop_column("orchestration_amendment_requests", "authoring_assignment")
