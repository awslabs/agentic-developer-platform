"""Retain revocable result-only consent independently of execution authority."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_wa_reuse_authorization"
down_revision = "l5_setup_wa_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_service_wa_reuse_authorizations",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("authorization_id", sa.String(36), primary_key=True),
        sa.Column("result_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("scope", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id", "result_id"], ["l5_service_results.tenant_id", "l5_service_results.result_id"]),
    )
    op.create_index("ix_l5_wa_reuse_owner", "l5_service_wa_reuse_authorizations", ["tenant_id", "result_id", "actor_id"])


def downgrade():
    require_empty("l5_service_wa_reuse_authorizations")
    op.drop_table("l5_service_wa_reuse_authorizations")
