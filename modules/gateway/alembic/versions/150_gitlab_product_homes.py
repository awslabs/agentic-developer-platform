"""Durable Product GitLab home bindings; credentials remain in Secrets Manager."""

import sqlalchemy as sa

from alembic import op

revision = "150_gitlab_product_homes"
down_revision = "149_merge_l5_project_budgets"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("product_gitlab_homes"):
        return
    op.create_table(
        "product_gitlab_homes",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("product_id", sa.String(36), primary_key=True),
        sa.Column("instance_url", sa.String(512), nullable=False),
        sa.Column("group_id", sa.BigInteger()),
        sa.Column("full_path", sa.String(512)),
        sa.Column("status", sa.String(24), nullable=False, server_default="preparing"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("membership_digest", sa.String(64)),
        sa.Column("members", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
    )
    op.create_index("ix_gitlab_home_reconcile", "product_gitlab_homes", ["checked_at", "tenant_id", "product_id"])


def downgrade():
    # Retain provider bindings on a code rollback; losing them risks duplicate homes.
    pass
