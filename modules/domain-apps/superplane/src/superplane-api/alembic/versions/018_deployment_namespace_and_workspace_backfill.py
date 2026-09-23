"""Record each deployment's namespace, and backfill workspace namespaces.

Issue #5671 (A15). Two changes, both prerequisites for the API to stop taking the
target namespace from the caller:

1. ``deployments.namespace`` — where the object was actually placed. A delete has to
   look where the object IS, not where today's configuration says it would go; without
   a recorded value the only alternative is recomputing, which silently targets the
   wrong namespace for any deployment created before a namespace assignment changed.

2. ``workspaces.namespace_name`` backfilled for rows that have none. The API resolves
   the namespace from this column and FAILS CLOSED when it cannot — deliberately, since
   the alternative is falling back to the shared ``default`` namespace, which is the
   cross-tenant exposure being fixed. Failing closed without this backfill would take
   away deploy/list/delete from tenants whose rows predate the column being populated,
   so the backfill is what keeps the fix from becoming an outage for them.

The backfill uses the same deterministic ``ws-<workspace id>`` rule as
``app.services.workspace_namespace.derive_namespace_name``; the two must agree, or a
workspace resolves to one namespace here and another at runtime.

Additive and safe to re-run. The downgrade drops the column but does NOT clear the
backfilled namespaces: they describe where workloads actually are, and erasing that
would orphan every object on the clusters.

Revision ID: 018_deployment_namespace_and_workspace_backfill
Revises: 017_add_workspace_bootstrap_reservations
"""

import sqlalchemy as sa
from alembic import op

revision = "018_deployment_namespace_and_workspace_backfill"
down_revision = "017_add_workspace_bootstrap_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("namespace", sa.String(length=255), nullable=True),
    )

    # Backfill workspaces that have no recorded namespace. Only NULL/blank rows are
    # touched, so an existing assignment is never overwritten — a workspace whose
    # namespace was set by bootstrap keeps it.
    op.execute(
        """
        UPDATE workspaces
           SET namespace_name = 'ws-' || id
         WHERE namespace_name IS NULL
            OR trim(namespace_name) = ''
        """
    )

    # Deployment lookups during quota accounting and delete are keyed on workspace_id;
    # the quota path sums over a workspace's live deployments on every create.
    op.create_index(
        "ix_deployments_workspace_status",
        "deployments",
        ["workspace_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_deployments_workspace_status", table_name="deployments")
    op.drop_column("deployments", "namespace")
