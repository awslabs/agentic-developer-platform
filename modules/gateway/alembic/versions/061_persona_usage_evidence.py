"""Add persona-policy and pricing evidence to usage_logs.

Issue #5426 (PMM-08).  Every column is nullable, has no server default and is
never backfilled. PostgreSQL adds this shape as metadata-only, with a one-second
lock wait and a bounded transaction. Existing-ledger indexes are built
concurrently in revision 062. The release migrates before serving new code.

Old and new gateway pods can write concurrently: old pods omit these columns,
while new pods write them only when protected authority/pricing evidence exists.
NULL means "not captured", never an inferred persona, owner, chain or active
pricing version.

Revision ID: 061_persona_usage_evidence
Revises: 060_orch_pending_amend
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "061_persona_usage_evidence"
down_revision: str | None = "060_orch_pending_amend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS = (
    sa.Column("pricing_confidence", sa.String(32), nullable=True),
    sa.Column("pricing_estimate_reasons", sa.Text(), nullable=True),
    sa.Column("pricing_decision", sa.JSON(), nullable=True),
    sa.Column("model_decision", sa.JSON(), nullable=True),
    sa.Column("model_decision_id", sa.String(64), nullable=True),
    sa.Column("approving_human_id", sa.String(255), nullable=True),
    sa.Column("destination_region", sa.String(64), nullable=True),
    sa.Column("provider_request_id", sa.String(255), nullable=True),
    sa.Column("persona_key", sa.String(length=64), nullable=True),
    sa.Column("compatibility_class", sa.String(length=64), nullable=True),
    sa.Column("harness_contract_revision", sa.String(length=64), nullable=True),
    sa.Column("root_invocation_id", sa.String(length=255), nullable=True),
    sa.Column("chain_id", sa.String(length=255), nullable=True),
    sa.Column("preference_owner_kind", sa.String(length=32), nullable=True),
    sa.Column("preference_owner_id", sa.String(length=255), nullable=True),
    sa.Column("model_policy_snapshot_digest", sa.String(length=64), nullable=True),
    sa.Column("model_policy_revision", sa.String(length=64), nullable=True),
    sa.Column("model_catalogue_revision", sa.String(length=64), nullable=True),
    sa.Column("requested_model_id", sa.String(length=255), nullable=True),
    sa.Column("resolved_model_id", sa.String(length=255), nullable=True),
    sa.Column("resolution_source", sa.String(length=32), nullable=True),
    sa.Column("runtime_posture", sa.String(length=32), nullable=True),
    sa.Column("posture_revision", sa.Integer(), nullable=True),
    sa.Column("pricing_source_kind", sa.String(length=32), nullable=True),
    sa.Column("pricing_generation_id", sa.BigInteger(), nullable=True),
    sa.Column("pricing_pointer_revision", sa.BigInteger(), nullable=True),
    sa.Column("pricing_snapshot_version", sa.String(length=255), nullable=True),
    sa.Column("pricing_policy_version", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '1s'")
        op.execute("SET LOCAL statement_timeout = '30s'")
    for column in _COLUMNS:
        op.add_column("usage_logs", column)

    op.create_table(
        "persona_model_retirement_alerts",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("org_id", sa.String(length=255), nullable=False),
        sa.Column("preference_id", sa.String(length=255), nullable=False),
        sa.Column("persona_key", sa.String(length=64), nullable=False),
        sa.Column("preference_owner_kind", sa.String(length=32), nullable=False),
        sa.Column("preference_owner_id", sa.String(length=255), nullable=False),
        sa.Column("canonical_model_id", sa.String(length=255), nullable=False),
        sa.Column("lifecycle_revision", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="claimed", nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('claimed', 'delivered')", name="ck_persona_retirement_state"),
        sa.CheckConstraint("attempt_count >= 1", name="ck_persona_retirement_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "preference_id",
            "canonical_model_id",
            "lifecycle_revision",
            name="uq_persona_retirement_transition",
        ),
    )
    op.create_index("ix_persona_model_retirement_alerts_org_id", "persona_model_retirement_alerts", ["org_id"])
    op.create_index(
        "ix_persona_retirement_claim",
        "persona_model_retirement_alerts",
        ["state", "lease_expires_at"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '1s'")
        op.execute("SET LOCAL statement_timeout = '30s'")
    op.drop_index("ix_persona_retirement_claim", table_name="persona_model_retirement_alerts")
    op.drop_index("ix_persona_model_retirement_alerts_org_id", table_name="persona_model_retirement_alerts")
    op.drop_table("persona_model_retirement_alerts")
    for column in reversed(_COLUMNS):
        op.drop_column("usage_logs", column.name)
