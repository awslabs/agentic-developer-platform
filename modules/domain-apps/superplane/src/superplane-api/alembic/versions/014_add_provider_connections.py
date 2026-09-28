"""Add the provider_connections and provider_connection_bindings tables.

Issue #5053 (U7b), EPIC #4910 — R7 server half.

Creates the two server-held records the provider-connection routes read: the
connection (which credential reference, whose vault ownership, what the last
validation measured) and its binding (which one workspace may use it). See
``app/models/provider_connection.py`` for why they are two tables and not one.

WHAT THIS REVISION DELIBERATELY DOES NOT DO
-------------------------------------------
It creates tables and populates nothing. There is no backfill from
``credential_registry``, for the same reason revision ``012`` refuses to map
existing rows: an ARN and an ADP credential id are not translations of each
other, and a vault ownership record cannot be derived from either. Deriving a
connection row here would assert an ownership fact nobody established and hand it
to ``authorize_delegation`` as evidence — the "delegates a credential they were
never authorized to share" failure, manufactured by a migration.

Establishing those facts needs authorized vault/KMS access against a selected
account, which R7's acceptances 6-7 hold open under an unresolved gate. So this
revision leaves the tables empty and the routes create rows only from a live
vault response.

WHY THE DOWNGRADE IS SAFE
-------------------------
``downgrade()`` drops only the two tables this revision created and touches no
other table, so it cannot repopulate a secret ARN — neither table ever held one
(the reference columns refuse ARN-shaped values at the model, and nothing in this
revision writes a value at all).

Revision ID: 014_add_provider_connections
Revises: 013_add_provider_operations
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "014_add_provider_connections"
down_revision = "013_add_provider_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        # The vault's opaque handle. Never an ARN and never a value: the model's
        # validator refuses both, reusing U13b's hardened rule rather than a
        # second weaker copy.
        sa.Column("adp_credential_id", sa.String(length=255), nullable=False),
        # The vault's non-secret metadata, carried so a listing needs no second
        # vault lookup. Neither field can read the credential.
        sa.Column("credential_service", sa.String(length=100), nullable=False),
        sa.Column("credential_label", sa.String(length=255), nullable=False),
        # The vault-recorded owner. Written from the vault's own response, never
        # from a request body — a body-supplied owner would be the claim under
        # test, asserted by the party being tested.
        sa.Column("owner_principal", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        # The four validation readings as four columns. Separate because R7
        # acceptance 3 is that they stay separate: a valid key says nothing about
        # whether the provider has a free GPU.
        #
        # All four are NULLABLE, and `observed_capacity` especially so: "not
        # measured" is a different operational fact from "measured as zero", and a
        # NOT NULL column defaulting to 0 would erase that distinction in storage
        # after the contract went to trouble to preserve it.
        sa.Column("credential_valid", sa.Boolean(), nullable=True),
        sa.Column("permissions_sufficient", sa.Boolean(), nullable=True),
        sa.Column("quota_available", sa.Boolean(), nullable=True),
        sa.Column("observed_capacity", sa.Integer(), nullable=True),
        sa.Column(
            "validation_detail",
            sa.String(length=1024),
            nullable=False,
            server_default="",
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        # What disablement did NOT accomplish, stored so the disable response
        # surfaces it. Acceptance 5 is about an operator seeing this.
        sa.Column(
            "limitation", sa.String(length=512), nullable=False, server_default=""
        ),
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
        # One connection row per (org, credential). Two rows for one credential
        # would give two connection ids, so rotating through one leaves the other
        # pointing at a superseded reference while still reporting itself active.
        sa.UniqueConstraint(
            "org_id",
            "adp_credential_id",
            name="uq_provider_connections_org_credential",
        ),
    )
    op.create_index(
        "ix_provider_connections_org_id", "provider_connections", ["org_id"]
    )

    op.create_table(
        "provider_connection_bindings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("provider_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalized from the connection, as workspace_grants denormalizes its
        # org: the decision path compares the two, so a binding written against
        # the wrong credential is caught by a mismatch rather than by trusting a
        # join to have been right.
        sa.Column("adp_credential_id", sa.String(length=255), nullable=False),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("bound_by", sa.String(length=255), nullable=False),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # THE constraint, and the reason this is a database rule and not only a
        # model rule. The contract's `WorkspaceBinding` guarantees "exactly one
        # workspace" by being a two-scalar frozen dataclass, which stops nobody
        # from writing two ROWS — and then `authorize_use` is called with
        # whichever one the query returned first, letting row order decide a
        # tenant-isolation question. Declared here so a backfill, a reconciler or
        # a fixture that never crosses a route cannot produce the second row.
        sa.UniqueConstraint(
            "connection_id",
            name="uq_provider_connection_bindings_connection",
        ),
    )
    op.create_index(
        "ix_provider_connection_bindings_connection_id",
        "provider_connection_bindings",
        ["connection_id"],
    )
    op.create_index(
        "ix_provider_connection_bindings_workspace_id",
        "provider_connection_bindings",
        ["workspace_id"],
    )


def downgrade() -> None:
    # Bindings first: they carry the foreign key onto connections.
    op.drop_index(
        "ix_provider_connection_bindings_workspace_id",
        table_name="provider_connection_bindings",
    )
    op.drop_index(
        "ix_provider_connection_bindings_connection_id",
        table_name="provider_connection_bindings",
    )
    op.drop_table("provider_connection_bindings")
    op.drop_index("ix_provider_connections_org_id", table_name="provider_connections")
    op.drop_table("provider_connections")
