"""Add persona-policy and pricing evidence to usage_logs.

Issue #5426 (PMM-08).  Every column is nullable, has no server default and is
never backfilled.  PostgreSQL 11+ adds this shape as metadata-only; the two
partial indexes still require an index build and their production lock duration
must be measured before this draft can be promoted.

Old and new gateway pods can write concurrently: old pods omit these columns,
while new pods write them only when protected authority/pricing evidence exists.
NULL means "not captured", never an inferred persona, owner, chain or active
pricing version.

Revision ID: 059_persona_usage_evidence
Revises: 058_model_probe_admission
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "059_persona_usage_evidence"
down_revision: str | None = "058_model_probe_admission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMNS = (
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
    sa.Column("pricing_source_kind", sa.String(length=32), nullable=True),
    sa.Column("pricing_generation_id", sa.BigInteger(), nullable=True),
    sa.Column("pricing_pointer_revision", sa.BigInteger(), nullable=True),
    sa.Column("pricing_snapshot_version", sa.String(length=255), nullable=True),
    sa.Column("pricing_policy_version", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column("usage_logs", column)

    # Cost reads are tenant + preference owner + persona, never a global scan.
    op.create_index(
        "ix_usage_persona_owner",
        "usage_logs",
        ["org_id", "preference_owner_kind", "preference_owner_id", "persona_key"],
        postgresql_where=sa.text("persona_key IS NOT NULL AND preference_owner_id IS NOT NULL"),
        sqlite_where=sa.text("persona_key IS NOT NULL AND preference_owner_id IS NOT NULL"),
    )
    op.create_index(
        "ix_usage_chain_id",
        "usage_logs",
        ["org_id", "chain_id"],
        postgresql_where=sa.text("chain_id IS NOT NULL"),
        sqlite_where=sa.text("chain_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_usage_chain_id", table_name="usage_logs")
    op.drop_index("ix_usage_persona_owner", table_name="usage_logs")
    for column in reversed(_COLUMNS):
        op.drop_column("usage_logs", column.name)
