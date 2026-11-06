"""Persist public result projections and native findings.

Revision ID: l5_results_and_triage
Revises: l5_devops_resource_observation
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_results_and_triage"
down_revision = "l5_devops_resource_observation"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_result_details",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("result_id", sa.String(36), primary_key=True),
        sa.Column("payload", payload, nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "result_id"],
                                ["l5_service_results.tenant_id", "l5_service_results.result_id"]),
    )
    op.create_table(
        "l5_service_findings",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("finding_id", sa.String(36), primary_key=True),
        sa.Column("result_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("native_id", sa.String(255), nullable=False),
        sa.Column("payload", payload, nullable=False),
        sa.Column("triage_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["tenant_id", "result_id"],
                                ["l5_service_results.tenant_id", "l5_service_results.result_id"]),
        sa.UniqueConstraint("tenant_id", "result_id", "kind", "native_id"),
    )
    op.create_table(
        "l5_service_triage",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("finding_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", payload, nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "finding_id"],
                                ["l5_service_findings.tenant_id", "l5_service_findings.finding_id"]),
    )


def downgrade():
    op.drop_table("l5_service_triage")
    op.drop_table("l5_service_findings")
    op.drop_table("l5_service_result_details")
