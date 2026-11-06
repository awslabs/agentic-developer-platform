"""Add reversible archive state to delivery flows."""

import sqlalchemy as sa

from alembic import op

revision = "096_flow_archive"
down_revision = "095_crew_restore_pins"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orchestration_flows", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_orchestration_flows_org_archived_created", "orchestration_flows", ["org_id", "archived_at", "created_at"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM orchestration_flows WHERE archived_at IS NOT NULL)")):
        raise RuntimeError("Unarchive or reconcile archived flows before removing archive protection")
    op.drop_index("ix_orchestration_flows_org_archived_created", table_name="orchestration_flows")
    op.drop_column("orchestration_flows", "archived_at")
