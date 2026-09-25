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

revision = "035_shared_cluster_membership"
down_revision = "034_provider_request_region"
branch_labels = None
depends_on = None

_TABLE = "cluster_memberships"


def upgrade():
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
        sa.Column(
            "state", sa.String(32), nullable=False, server_default="reserved"
        ),
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
        # One non-removed membership per workspace (DESIGN.md §3.2: "exactly one
        # active membership for an execution-ready workspace"). `state` is part of
        # the unique key rather than a partial index predicate so this reads
        # identically under SQLite in the offline test lane, which has no
        # `postgresql_where` support — the Postgres-only tests additionally
        # exercise the *reserved+active-both-live* race this alone cannot catch.
        sa.UniqueConstraint(
            "workspace_id", "state", name="uq_cluster_memberships_workspace_live"
        ),
    )
    op.create_index(
        "ix_cluster_memberships_org_id", _TABLE, ["org_id"]
    )
    op.create_index(
        "ix_cluster_memberships_cluster_id", _TABLE, ["cluster_id"]
    )
    op.create_index(
        "ix_cluster_memberships_workspace_id", _TABLE, ["workspace_id"]
    )
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


def downgrade():
    op.drop_index(
        "uq_cluster_memberships_cluster_namespace_live", table_name=_TABLE
    )
    op.drop_index("ix_cluster_memberships_workspace_id", table_name=_TABLE)
    op.drop_index("ix_cluster_memberships_cluster_id", table_name=_TABLE)
    op.drop_index("ix_cluster_memberships_org_id", table_name=_TABLE)
    op.drop_table(_TABLE)
    op.drop_column("clusters", "platform_eligible")
    op.drop_column("clusters", "sharing_enabled")
