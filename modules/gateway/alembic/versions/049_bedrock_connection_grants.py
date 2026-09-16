"""Explicit organization Bedrock grants over existing AWS connections."""

import sqlalchemy as sa

from alembic import op

revision = "049_bedrock_connection_grants"
down_revision = "048_team_integrity_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bedrock_connection_grants",
        sa.Column("destination_id", sa.String(255), sa.ForeignKey("bedrock_destination_registry.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("created_by_user_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("credential_id", "org_id", name="uq_bedrock_connection_grant_scope"),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT count(*) FROM bedrock_connection_grants")).scalar_one():
        raise RuntimeError("Remove existing-connection Bedrock links and their routing rules before downgrading.")
    op.drop_table("bedrock_connection_grants")
