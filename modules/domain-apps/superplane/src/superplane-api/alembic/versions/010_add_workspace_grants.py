"""Add the workspace_grants table.

Issue #5055 (U14). Creates the server-held record that a named principal holds
named permissions on one workspace — the thing the authorization check reads at
the moment an operation runs. See ``app/models/workspace_grant.py`` for why the
table exists and why permissions are stored as the policy's permission values
rather than as a role name.

U13 (#5045) repaired the previously unloadable graph before this story merged.
This revision therefore extends its single head rather than the historical
``006_account_onboard_fields`` leaf. The migration is unapplied, so renumbering
it during conflict resolution preserves every deployed revision while keeping
fresh installs on one linear chain.

Revision ID: 010_add_workspace_grants
Revises: 009_add_budget_alerts_table
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "010_add_workspace_grants"
down_revision = "009_add_budget_alerts_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_grants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        # Denormalized from the workspace on purpose: the decision path compares
        # this against both the workspace's stored org and the token's verified
        # org claim, so a grant written against the wrong org is caught by a
        # mismatch instead of by trusting a join to have been correct.
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        # The verified token subject. Opaque; never parsed for meaning.
        sa.Column("principal", sa.String(length=255), nullable=False),
        sa.Column(
            "principal_type",
            sa.String(length=32),
            nullable=False,
            server_default="human",
        ),
        # Space-separated values from superplane_auth.policy.Permission.
        sa.Column("permissions", sa.Text(), nullable=False, server_default=""),
        # NULL means active. Re-read per operation, which is what makes
        # revocation between sign-in and execution deny the operation.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        # One row per (workspace, principal). Two rows would make the effective
        # permission set depend on row order, which is a silent widening: the
        # union of two partial grants is not what either of them says. Enforced
        # in the database rather than only in the model, because the model is
        # not the only writer a database outlives.
        sa.UniqueConstraint(
            "workspace_id", "principal", name="uq_workspace_grants_workspace_principal"
        ),
    )
    # Indexes match the three columns the authorization queries filter on: the
    # per-operation workspace lookup, and the org-scoped lookup that reads every
    # grant a principal holds.
    op.create_index(
        "ix_workspace_grants_workspace_id", "workspace_grants", ["workspace_id"]
    )
    op.create_index("ix_workspace_grants_org_id", "workspace_grants", ["org_id"])
    op.create_index("ix_workspace_grants_principal", "workspace_grants", ["principal"])


def downgrade() -> None:
    op.drop_index("ix_workspace_grants_principal", table_name="workspace_grants")
    op.drop_index("ix_workspace_grants_org_id", table_name="workspace_grants")
    op.drop_index("ix_workspace_grants_workspace_id", table_name="workspace_grants")
    op.drop_table("workspace_grants")
