"""Add provider operations, durable reference conflicts and allocation membership.

Revision ID: 013_add_provider_operations
Revises: 012_adp_credential_reference
Create Date: 2026-09-18

Issue #5054 (U11c), EPIC #4910. Additive only: four new tables, no column added to
or removed from an existing one, and no data migration. Nothing already in the
database changes meaning, so applying this before any caller records a handle is
safe, and downgrading afterwards drops only rows this story's endpoints wrote.

## Why the primary key is (workspace, idempotency_key)

Explained at length in ``app/models/provider_handle.py``: the question asked after
a crash is "did I already start this operation?", and the only identity the asker
has is the one it computed before the call. A surrogate id would let a retry after
a lost response insert a second row for one operation, which is the duplicate the
table exists to prevent.

The workspace is part of that key because the conflict *is* the duplicate
detection, and the keys the in-repo callers generate are derived strings like
``sp-aws-a100-1`` that contain no tenant. Keyed on the idempotency key alone, one
workspace's row would refuse another workspace's unrelated operation. This
revision has never been applied anywhere, so the key is defined correctly here
rather than corrected later by a migration that would have to rewrite a primary
key on live rows.

**It has not been applied anywhere.** U11c authors the schema; U23 (#5327) owns
deployment, and merging this story authorizes no migration run. The offline
revision graph stays single-headed: this extends ``012_adp_credential_reference``,
which U13b left as the sole head.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "013_add_provider_operations"
down_revision = "012_adp_credential_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_allocations",
        sa.Column(
            "workspace",
            UUID(as_uuid=False),
            sa.ForeignKey("workspaces.id"),
            primary_key=True,
        ),
        sa.Column("allocation_id", sa.String(255), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
    )
    op.create_table(
        "provider_operations",
        sa.Column("idempotency_key", sa.String(255), primary_key=True),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("resource_name", sa.String(255), nullable=False),
        sa.Column("allocation_id", sa.String(255), nullable=False),
        # Second half of the primary key — see the module docstring. Scoping the
        # idempotency identity to the workspace is what stops one tenant's derived
        # key from refusing another tenant's operation.
        sa.Column(
            "workspace",
            UUID(as_uuid=False),
            sa.ForeignKey("workspaces.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        # Absent until the provider answers — the ordering rule this table exists
        # to enforce means the row is written before any reference exists.
        sa.Column("authority_operation_id", sa.String(255), nullable=False),
        sa.Column("authority_run_id", sa.String(255), nullable=False),
        sa.Column("authority_attempt_id", sa.String(255), nullable=False),
        sa.Column("provider_reference", sa.String(255), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reconcile_result", sa.String(32), nullable=True),
        sa.Column("provider_presence", sa.String(32), nullable=True),
        # Provider state is free-form at the API boundary; retain the complete
        # report rather than losing a durable conclusion to VARCHAR truncation.
        sa.Column("provider_state", sa.Text(), nullable=True),
        sa.Column("observation_queried_by", sa.String(255), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace", "allocation_id"],
            ["provider_allocations.workspace", "provider_allocations.allocation_id"],
        ),
    )
    op.create_table(
        "provider_reference_conflicts",
        sa.Column("workspace", UUID(as_uuid=False), primary_key=True),
        sa.Column("idempotency_key", sa.String(255), primary_key=True),
        sa.Column("provider_reference", sa.String(255), primary_key=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("provider_presence", sa.String(32)),
        sa.Column("provider_state", sa.Text()),
        sa.Column("observation_queried_by", sa.String(255)),
        sa.Column(
            "reported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace", "idempotency_key"],
            ["provider_operations.workspace", "provider_operations.idempotency_key"],
        ),
    )
    op.create_table(
        "provider_allocation_resources",
        sa.Column(
            "workspace",
            UUID(as_uuid=False),
            sa.ForeignKey("workspaces.id"),
            primary_key=True,
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=False),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("allocation_id", sa.String(255), primary_key=True),
        sa.Column("resource_id", sa.String(255), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("provider_reference", sa.String(255), nullable=False),
        sa.Column("resource_kind", sa.String(64), nullable=False),
        sa.Column("operation_keys", sa.JSON(), nullable=False),
        sa.Column("inventory_revision", sa.String(255), nullable=False),
    )
    op.create_index(
        "ix_provider_operations_allocation_id", "provider_operations", ["allocation_id"]
    )
    op.create_index(
        "ix_provider_operations_workspace", "provider_operations", ["workspace"]
    )
    op.create_index("ix_provider_operations_state", "provider_operations", ["state"])
    # The recovery read filters on workspace and state together.
    op.create_index(
        "ix_provider_operations_workspace_state",
        "provider_operations",
        ["workspace", "state"],
    )


def downgrade() -> None:
    op.drop_table("provider_allocation_resources")
    op.drop_table("provider_reference_conflicts")
    op.drop_index("ix_provider_operations_workspace_state", "provider_operations")
    op.drop_index("ix_provider_operations_state", "provider_operations")
    op.drop_index("ix_provider_operations_workspace", "provider_operations")
    op.drop_index("ix_provider_operations_allocation_id", "provider_operations")
    op.drop_table("provider_operations")
    op.drop_table("provider_allocations")
