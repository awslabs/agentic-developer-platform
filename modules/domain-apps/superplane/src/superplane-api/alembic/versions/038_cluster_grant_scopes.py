"""Explicit cluster scopes of existing organization grants; no inferred grants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "038_cluster_grant_scopes"
down_revision = "037_shared_cluster_membership"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_organization_grants_org_identity", "organization_grants", ["org_id", "id"]
    )
    op.create_table(
        "organization_grant_cluster_scopes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permissions", sa.Text(), nullable=False),
        sa.Column("generation", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "grant_id", "cluster_id", name="uq_org_grant_cluster_scope"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "grant_id"],
            ["organization_grants.org_id", "organization_grants.id"],
            name="fk_cluster_scope_org_grant",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "cluster_id"],
            ["clusters.org_id", "clusters.id"],
            name="fk_cluster_scope_org_cluster",
        ),
    )


def downgrade():
    # Keep the history check and destructive DDL atomic with concurrent writers.
    op.execute("LOCK TABLE organization_grant_cluster_scopes IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM organization_grant_cluster_scopes) "
        "THEN RAISE EXCEPTION 'cluster scope history must be explicitly preserved before rollback'; "
        "END IF; END $$"
    )
    op.drop_table("organization_grant_cluster_scopes")
    op.drop_constraint(
        "uq_organization_grants_org_identity", "organization_grants", type_="unique"
    )
