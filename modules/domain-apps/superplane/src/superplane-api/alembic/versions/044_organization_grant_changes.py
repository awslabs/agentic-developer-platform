"""Revisioned organization grants and a durable, tenant-bound mutation ledger."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "044_organization_grant_changes"
down_revision = "043_workspace_grant_changes"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "organization_grants",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "organization_grant_changes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("grant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "event_id", UUID(as_uuid=True), sa.ForeignKey("events.id"), nullable=False
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "grant_id"],
            ["organization_grants.org_id", "organization_grants.id"],
            name="fk_org_grant_change_tenant",
        ),
        sa.UniqueConstraint("org_id", "request_id", name="uq_org_grant_change_request"),
        sa.UniqueConstraint(
            "grant_id", "revision", name="uq_org_grant_change_revision"
        ),
    )


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM organization_grant_changes)
        THEN RAISE EXCEPTION 'organization grant change evidence must be retained'; END IF; END $$""")
    op.drop_table("organization_grant_changes")
    op.drop_column("organization_grants", "revision")
