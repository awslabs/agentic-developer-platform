"""Store owner-scoped conversation metadata, never transcript bodies."""

import sqlalchemy as sa

from alembic import op

revision = "132_crew_conversations"
down_revision = "131_legacy_binding_migration"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crew_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("checkpoint_id", sa.String(36), sa.ForeignKey("crew_checkpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column("archive_path", sa.String(1024), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("unreadable", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("checkpoint_id", "source", "conversation_id", name="uq_crew_conversation_checkpoint"),
    )
    op.create_index("ix_crew_conversations_owner_activity", "crew_conversations", ["tenant_id", "user_id", "last_activity_at"])


def downgrade():
    op.drop_index("ix_crew_conversations_owner_activity", table_name="crew_conversations")
    op.drop_table("crew_conversations")
