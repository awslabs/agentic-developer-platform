"""Explicit cluster sharing eligibility and per-workspace cluster membership.

Issue #6048, EPIC #4910. Adds the schema DESIGN.md §3.2 requires for shared
placement: a cluster is never shareable by inference (name, account, or having a
dedicated workspace already) — it must carry an explicit `sharing_enabled` flag,
and an ADP management cluster additionally needs `platform_eligible` before a
tenant workspace may join it. `cluster_memberships` is the new entity that
records one workspace's binding to one cluster, separately from `workspaces.cluster_id`
(kept as the existing compatibility projection) and separately from
`clusters.workspace_id` (kept as the legacy single-owner column canonical
registration and cost/observation authority already read).

This migration is purely additive: it adds two boolean columns (both default
false, so no existing cluster becomes shareable by running this) and one new
table. No existing read path changes, and no existing row's meaning changes.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "037_shared_cluster_membership"
down_revision = "036_users_cognito_sub_per_org"
branch_labels = None
depends_on = None

_TABLE = "cluster_memberships"


def upgrade():
    op.create_unique_constraint(
        "uq_clusters_org_identity", "clusters", ["org_id", "id"]
    )
    op.create_unique_constraint(
        "uq_workspaces_org_identity", "workspaces", ["org_id", "id"]
    )
    op.add_column(
        "clusters",
        sa.Column(
            "sharing_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "clusters",
        sa.Column(
            "platform_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=False,
        ),
        sa.Column("generation", sa.String(64), nullable=False),
        sa.Column("namespace", sa.String(255), nullable=False),
        sa.Column("namespace_uid", sa.String(255), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("credential_reference_id", sa.String(255), nullable=True),
        sa.Column("removal_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('reserved', 'active', 'removed')",
            name="ck_cluster_memberships_state",
        ),
        sa.CheckConstraint(
            "generation ~ '^[a-f0-9]{64}$'",
            name="ck_cluster_memberships_generation",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workspace_id"],
            ["workspaces.org_id", "workspaces.id"],
            name="fk_cluster_memberships_workspace_org",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "cluster_id"],
            ["clusters.org_id", "clusters.id"],
            name="fk_cluster_memberships_cluster_org",
        ),
    )
    op.create_index(
        "uq_cluster_memberships_workspace_live",
        _TABLE,
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'removed'"),
    )
    op.create_index("ix_cluster_memberships_org_id", _TABLE, ["org_id"])
    op.create_index("ix_cluster_memberships_cluster_id", _TABLE, ["cluster_id"])
    op.create_index("ix_cluster_memberships_workspace_id", _TABLE, ["workspace_id"])
    # Unique non-retired (cluster_id, namespace) binding (DESIGN.md §3.2). Only
    # enforced across live (non-removed) rows: a removed membership's namespace
    # name is not reserved forever, but two *live* memberships on one cluster must
    # never collide on namespace.
    op.create_index(
        "uq_cluster_memberships_cluster_namespace_live",
        _TABLE,
        ["cluster_id", "namespace"],
        unique=True,
        postgresql_where=sa.text("state <> 'removed'"),
    )
    # Secret bytes stay in the two Kubernetes projections. This journal retains
    # only exact issuance/rotation/revocation identity and observed delivery.
    op.create_table(
        "membership_credentials",
        sa.Column(
            "membership_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cluster_memberships.id"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("namespace_uid", sa.String(255), nullable=False),
        sa.Column("service_account_uid", sa.String(255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("projection_uid", sa.String(255), nullable=True),
        sa.Column("projection_namespace", sa.String(63), nullable=True),
        sa.Column("projection_namespace_uid", sa.String(255), nullable=True),
        sa.Column("projection_name", sa.String(253), nullable=True),
        sa.Column("content_digest", sa.String(64), nullable=True),
        sa.Column("projection_version", sa.String(255), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "revision BETWEEN 1 AND 2147483647",
            name="ck_membership_credentials_revision",
        ),
        sa.CheckConstraint(
            "scope IN ('reader','mutator')", name="ck_membership_credentials_scope"
        ),
        sa.CheckConstraint(
            "state IN ('reserved','issued','projected','active','revoking','revoked')",
            name="ck_membership_credentials_state",
        ),
        sa.CheckConstraint(
            "state IN ('reserved','revoking','revoked') OR (service_account_uid IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_membership_credentials_issued",
        ),
        sa.CheckConstraint(
            "state NOT IN ('projected','active') OR (projection_uid IS NOT NULL AND projection_version IS NOT NULL AND projection_namespace IS NOT NULL AND projection_namespace_uid IS NOT NULL AND projection_name IS NOT NULL AND content_digest IS NOT NULL)",
            name="ck_membership_credentials_projected",
        ),
        sa.CheckConstraint(
            "state <> 'active' OR observed_at IS NOT NULL",
            name="ck_membership_credentials_observed",
        ),
    )
    op.create_index(
        "uq_membership_credentials_active",
        "membership_credentials",
        ["membership_id", "scope"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_table(
        "cluster_credential_authorities",
        sa.Column("authority_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "cluster_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=False,
        ),
        sa.Column("document_json", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("holder", sa.Text(), nullable=True),
        sa.Column("fence_token", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "org_id", "cluster_id", name="uq_cluster_credential_authorities_cluster"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "cluster_id"],
            ["clusters.org_id", "clusters.id"],
            name="fk_cluster_credential_authorities_org",
        ),
        sa.CheckConstraint(
            "fence_token >= 0", name="ck_cluster_credential_authorities_fence"
        ),
        sa.CheckConstraint(
            "(holder IS NULL) = (lease_expires_at IS NULL)",
            name="ck_cluster_credential_authorities_lease",
        ),
    )
    op.create_table(
        "membership_credential_components",
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(253), primary_key=True),
        sa.Column("desired_json", sa.Text(), nullable=False),
        sa.Column("identity_json", sa.Text(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="planned"),
        sa.ForeignKeyConstraint(
            ["membership_id", "revision", "scope"],
            [
                "membership_credentials.membership_id",
                "membership_credentials.revision",
                "membership_credentials.scope",
            ],
        ),
        sa.CheckConstraint(
            "kind IN ('ServiceAccount','Role','RoleBinding')",
            name="ck_membership_credential_components_kind",
        ),
        sa.CheckConstraint(
            "state IN ('planned','created','revoked')",
            name="ck_membership_credential_components_state",
        ),
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM cluster_memberships) THEN
            RAISE EXCEPTION 'Retain membership identity and removal history before rollback';
        END IF;
    END $$""")
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM cluster_credential_authorities) THEN
            RAISE EXCEPTION 'Retain cluster credential authority before rollback';
        END IF;
    END $$""")
    op.drop_table("membership_credential_components")
    op.drop_table("cluster_credential_authorities")
    op.drop_index(
        "uq_membership_credentials_active", table_name="membership_credentials"
    )
    op.drop_table("membership_credentials")
    op.drop_index("uq_cluster_memberships_workspace_live", table_name=_TABLE)
    op.drop_index("uq_cluster_memberships_cluster_namespace_live", table_name=_TABLE)
    op.drop_index("ix_cluster_memberships_workspace_id", table_name=_TABLE)
    op.drop_index("ix_cluster_memberships_cluster_id", table_name=_TABLE)
    op.drop_index("ix_cluster_memberships_org_id", table_name=_TABLE)
    op.drop_table(_TABLE)
    op.drop_column("clusters", "platform_eligible")
    op.drop_column("clusters", "sharing_enabled")
    op.drop_constraint("uq_workspaces_org_identity", "workspaces", type_="unique")
    op.drop_constraint("uq_clusters_org_identity", "clusters", type_="unique")
