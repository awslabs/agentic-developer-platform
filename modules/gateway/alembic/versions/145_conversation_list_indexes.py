"""Partial indexes for the My conversations list (#9026): only labelled rows are conversations."""

import sqlalchemy as sa

from alembic import op

revision = "145_conversation_list_indexes"
down_revision = "144_request_logs"
branch_labels = None
depends_on = None


def upgrade():
    # The list filters on `harness IS NOT NULL` first; on dev that is 8k of one owner's 350k rows.
    op.create_index(
        "ix_usage_conversation_user",
        "usage_logs",
        ["user_id", "timestamp"],
        postgresql_where=sa.text("harness IS NOT NULL"),
    )
    op.create_index(
        "ix_usage_conversation_owner",
        "usage_logs",
        ["owner_user_id", "timestamp"],
        postgresql_where=sa.text("harness IS NOT NULL AND owner_user_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("ix_usage_conversation_owner", table_name="usage_logs")
    op.drop_index("ix_usage_conversation_user", table_name="usage_logs")
