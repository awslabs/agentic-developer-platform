"""Retain the original FinOps question digest across worker leases.

Revision ID: l5_finops_inquiry_attempt
Revises: l5_finops_publication_merge
"""

from alembic import op
import sqlalchemy as sa


revision = "l5_finops_inquiry_attempt"
down_revision = "l5_finops_publication_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("l5_service_worker_attempts", sa.Column("question_hash", sa.String(64)))


def downgrade():
    op.drop_column("l5_service_worker_attempts", "question_hash")
