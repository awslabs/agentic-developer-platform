"""Grant revision and durable idempotency ledger for explicit human assignments."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "043_workspace_grant_changes"
down_revision = "042_controller_cleanup_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workspace_grants", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "workspace_grant_changes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("grant_id", UUID(as_uuid=True), sa.ForeignKey("workspace_grants.id"), nullable=False),
        sa.Column("event_id", UUID(as_uuid=True), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint("workspace_id", "request_id", name="uq_workspace_grant_change_request"),
    )


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM workspace_grant_changes)
        THEN RAISE EXCEPTION 'workspace grant change evidence must be retained'; END IF; END $$""")
    op.drop_table("workspace_grant_changes")
    op.drop_column("workspace_grants", "revision")
