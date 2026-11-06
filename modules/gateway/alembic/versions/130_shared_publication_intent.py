"""Retain an unsettled registry publication on the shared release lease."""

import sqlalchemy as sa

from alembic import op

revision = "130_shared_publication_intent"
down_revision = "129_merge_native_epic_atlassian"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orchestration_environment_leases", sa.Column("publication_pending_digest", sa.String(71), nullable=True))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM orchestration_environment_leases WHERE publication_pending_digest IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Unsettled shared publication must be reconciled before rollback")
    op.drop_column("orchestration_environment_leases", "publication_pending_digest")
