"""Conversation identity on usage_logs (#9024): harness, conversation_id, origin, title, owner."""

import sqlalchemy as sa

from alembic import op

revision = "142_conversation_identity"
down_revision = "141_normalize_environments"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("usage_logs", sa.Column("harness", sa.String(32), nullable=True))
    op.add_column("usage_logs", sa.Column("conversation_id", sa.String(128), nullable=True))
    op.add_column("usage_logs", sa.Column("origin", sa.String(16), nullable=True))
    op.add_column("usage_logs", sa.Column("conversation_title", sa.String(160), nullable=True))
    op.add_column("usage_logs", sa.Column("owner_user_id", sa.String(255), nullable=True))
    op.create_index(
        "ix_usage_conversation",
        "usage_logs",
        ["org_id", "conversation_id", "timestamp"],
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
    )
    op.create_index(
        "ix_usage_owner_time",
        "usage_logs",
        ["owner_user_id", "timestamp"],
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("ix_usage_owner_time", table_name="usage_logs")
    op.drop_index("ix_usage_conversation", table_name="usage_logs")
    for column in ("owner_user_id", "conversation_title", "origin", "conversation_id", "harness"):
        op.drop_column("usage_logs", column)
