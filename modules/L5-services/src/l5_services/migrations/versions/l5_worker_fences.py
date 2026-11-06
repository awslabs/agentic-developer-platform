"""Count readback attempts before calling the native provider.

Revision ID: l5_worker_fences
Revises: l5_worker_repair
"""

from alembic import op
import sqlalchemy as sa

revision = "l5_worker_fences"
down_revision = "l5_worker_repair"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("l5_service_worker_attempts", sa.Column("reconcile_checks", sa.Integer(),
                                                        nullable=False, server_default="0"))


def downgrade():
    op.drop_column("l5_service_worker_attempts", "reconcile_checks")
