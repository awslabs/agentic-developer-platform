"""Vault evidence substrate for operation-bound credential delivery — Issue #5528.

Wave 6 / w6-05. Creates the two tables the credential-evidence port needs and
the Gateway had no home for:

``credential_workspace_delegations``
    Which workspaces a credential's owner allowed it in. The contract's
    ``VaultOwnership.delegated_to_workspaces`` is read from here;
    ``authorize_delegation`` treats an unresolved ownership record as a denial, so
    without this table the only honest value was the empty set.

``credential_validation_evidence``
    The four provider-validation readings the Gateway independently holds, so the
    attestation digest can be RECOMPUTED rather than echoed from the request. The
    port's docstring is explicit that the caller's digest is "untrusted request
    context, not proof".

Both tables carry ``org_id`` (TenantMixin) and cascade from ``user_credentials``:
deleting a credential must not leave a delegation that outlives the thing it
delegates, which would otherwise read as an active grant to a dangling id.

Uniqueness is the point of both indexes. One (credential, workspace) pair has at
most one row, which is what makes "ambiguous" a refusable condition in the service
rather than a ranking decision — the vault's ``CredentialResolver`` deliberately
ranks and picks a winner, and that behaviour is wrong for exact binding.

Reversible: ``downgrade`` drops both tables. No existing table is altered and no
row is rewritten, so a revert restores the prior schema exactly.
"""

import sqlalchemy as sa

from alembic import op

# Abbreviated to fit alembic_version.version_num VARCHAR(32) — the full
# "066_credential_evidence_delegation" is 34 chars and would roll the migration
# back on Postgres. Asserted by tests/migrations/test_revision_id_length.py.
revision = "066_cred_evidence_delegation"
down_revision = "065_budget_enforcement_controls"
branch_labels = None
depends_on = None

_DELEGATIONS = "credential_workspace_delegations"
_VALIDATION = "credential_validation_evidence"


def upgrade() -> None:
    op.create_table(
        _DELEGATIONS,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column(
            "credential_id",
            sa.String(36),
            sa.ForeignKey("user_credentials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("delegated_by", sa.String(255), nullable=False),
        sa.Column("delegated_at", sa.DateTime(timezone=True), nullable=False),
        # NULL means active. A withdrawn delegation is retained rather than
        # deleted so a refusal can distinguish "never delegated" from "revoked".
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_credential_workspace_delegations_org_id", _DELEGATIONS, ["org_id"])
    op.create_index("ix_credential_workspace_delegations_credential_id", _DELEGATIONS, ["credential_id"])
    op.create_index(
        "ix_credential_workspace_delegations_org_id_workspace",
        _DELEGATIONS,
        ["org_id", "workspace_id"],
    )
    # THE constraint: exactly one delegation row per credential+workspace.
    op.create_index(
        "uq_credential_workspace_delegations_pair",
        _DELEGATIONS,
        ["credential_id", "workspace_id"],
        unique=True,
    )

    op.create_table(
        _VALIDATION,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column(
            "credential_id",
            sa.String(36),
            sa.ForeignKey("user_credentials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("validated_version_id", sa.String(255), nullable=True),
        sa.Column("provider_account_id", sa.String(255), nullable=True),
        # The four readings, stored separately. No aggregate column by design.
        sa.Column("credential_valid", sa.Boolean(), nullable=False),
        sa.Column("permissions_sufficient", sa.Boolean(), nullable=False),
        sa.Column("quota_available", sa.Boolean(), nullable=False),
        # Nullable AND 0-distinct: "not measured" is a different fact from
        # "measured as zero" and the contract refuses to collapse them.
        sa.Column("observed_capacity", sa.Integer(), nullable=True),
        # The PROVIDER's measurement time. The consumer refuses a future value.
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.String(1024), nullable=False, server_default=""),
        # Local receipt time, kept separate from the provider's checked_at.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_credential_validation_evidence_org_id", _VALIDATION, ["org_id"])
    op.create_index("ix_credential_validation_evidence_credential_id", _VALIDATION, ["credential_id"])
    op.create_index(
        "uq_credential_validation_evidence_pair",
        _VALIDATION,
        ["credential_id", "workspace_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_credential_validation_evidence_pair", table_name=_VALIDATION)
    op.drop_index("ix_credential_validation_evidence_credential_id", table_name=_VALIDATION)
    op.drop_index("ix_credential_validation_evidence_org_id", table_name=_VALIDATION)
    op.drop_table(_VALIDATION)

    op.drop_index("uq_credential_workspace_delegations_pair", table_name=_DELEGATIONS)
    op.drop_index("ix_credential_workspace_delegations_org_id_workspace", table_name=_DELEGATIONS)
    op.drop_index("ix_credential_workspace_delegations_credential_id", table_name=_DELEGATIONS)
    op.drop_index("ix_credential_workspace_delegations_org_id", table_name=_DELEGATIONS)
    op.drop_table(_DELEGATIONS)
