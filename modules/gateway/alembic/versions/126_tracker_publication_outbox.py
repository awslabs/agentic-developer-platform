"""Persist tracker writes across failed acknowledgments and restarts.

Revision ID: 126_tracker_publication_outbox
Revises: 125_flow_planning_references
"""

import sqlalchemy as sa

from alembic import op

revision = "126_tracker_publication_outbox"
down_revision = "125_flow_planning_references"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "orchestration_tracker_publications",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("flow_id", sa.String(36), sa.ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_id", sa.BigInteger(), nullable=False),
        sa.Column("epic_ref", sa.String(255), nullable=False),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("installation_id", sa.Integer(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("body_region", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("watermark", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_orchestration_tracker_publications_org_id", "orchestration_tracker_publications", ["org_id"])
    op.create_index("ix_orchestration_tracker_publications_owed", "orchestration_tracker_publications", ["completed_at", "created_at"])


def downgrade():
    op.drop_index("ix_orchestration_tracker_publications_org_id", table_name="orchestration_tracker_publications")
    op.drop_index("ix_orchestration_tracker_publications_owed", table_name="orchestration_tracker_publications")
    op.drop_table("orchestration_tracker_publications")
