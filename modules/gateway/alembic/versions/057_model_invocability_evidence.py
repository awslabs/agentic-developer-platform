"""Model invocability evidence store — Issue #5420 (PMM-03).

Design: ``docs/design-notes/5420-persona-and-model-catalogue.md`` §4.1a.

Creates ``model_invocability_evidence`` — the single authoritative store for
destination-aware, class-keyed, harness-versioned model invocability proofs.
Everything else that reports invocability is a reader of this table.

The five-part composite primary key (§4.1b) ensures one destination/model/
class/revision/shape combination has exactly one authoritative row:
  1. Destination: account_id + region
  2. Canonical model: canonical_model_id
  3. Compatibility class: compatibility_class
  4. Harness/contract revision: harness_contract_revision
  5. Actual request-shape revision: request_shape_sha256

Follows the ``044_model_pricing_v2`` pattern: CHECK constraints enforce
status/timestamp consistency so a row cannot read as proven without carrying
a request ID and a verified_at.
"""

import sqlalchemy as sa

from alembic import op

# Parented on main's 055, not 054.  Both this revision and
# 055_orch_environment_leases originally declared down_revision
# "054_execution_tenant_guards", which is a single head on either branch alone
# and *two* heads on the merge ref that CI actually runs — the failure mode a
# branch-local ``alembic heads`` cannot show you.
#
# NOTE FOR THE PMM-02 REBASE: PMM-02 (#5419) already owns
# "056_persona_model_prefs", also parented on 055_orch_environment_leases.
# Since PMM-02 merges first, this revision must become 057_* parented on
# "056_persona_model_prefs" at rebase time — renumbering to 056 now keeps this
# PR single-head against today's main, but 056 is not its final number.
revision = "057_model_invocability_evidence"
down_revision = "056_persona_model_prefs"
branch_labels = None
depends_on = None

# Table and constraint names
_TABLE = "model_invocability_evidence"
_PK = "pk_model_invocability_evidence"
_CK_SHA = "ck_evidence_sha256_hex"
_CK_STATUS = "ck_evidence_status_consistent"
_CK_OUTCOME = "ck_evidence_outcome_values"
_IX_MODEL = "ix_evidence_model_id"
_IX_EXPIRY = "ix_evidence_expires_at"

# Allowed outcome values
_OUTCOMES = ("proven", "refused", "error")
_OUTCOME_CHECK = " OR ".join(f"outcome = '{v}'" for v in _OUTCOMES)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        # --- Key part 1: Destination ---
        sa.Column("account_id", sa.String(12), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        # --- Key part 2: Canonical model ---
        sa.Column("canonical_model_id", sa.String(255), nullable=False),
        # --- Key part 3: Compatibility class ---
        sa.Column("compatibility_class", sa.String(64), nullable=False),
        # --- Key part 4: Harness/contract revision ---
        sa.Column("harness_contract_revision", sa.String(32), nullable=False),
        # --- Key part 5: Actual request-shape revision ---
        # SHA-256 hex digest of the canonicalised request body the probe sent.
        sa.Column("request_shape_sha256", sa.String(64), nullable=False),
        # --- Evidence payload ---
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # --- Constraints ---
        sa.PrimaryKeyConstraint(
            "account_id",
            "region",
            "canonical_model_id",
            "compatibility_class",
            "harness_contract_revision",
            "request_shape_sha256",
            name=_PK,
        ),
        # Outcome vocabulary
        sa.CheckConstraint(_OUTCOME_CHECK, name=_CK_OUTCOME),
        # A proven row must carry a provider_request_id (§4.1a status/timestamp
        # consistency CHECK, following 044_model_pricing_v2).
        sa.CheckConstraint(
            "outcome != 'proven' OR provider_request_id IS NOT NULL",
            name=_CK_STATUS,
        ),
    )

    # SHA-256 hex format CHECK — SQLite does not support regex CHECK, so
    # only apply on PostgreSQL.
    dialect = op.get_context().dialect.name
    if dialect != "sqlite":
        op.create_check_constraint(
            _CK_SHA,
            _TABLE,
            sa.text("request_shape_sha256 ~ '^[0-9a-f]{64}$'"),
        )

    # Indexes for common read paths
    op.create_index(_IX_MODEL, _TABLE, ["canonical_model_id"])
    op.create_index(_IX_EXPIRY, _TABLE, ["expires_at"])


def downgrade() -> None:
    op.drop_index(_IX_EXPIRY, table_name=_TABLE)
    op.drop_index(_IX_MODEL, table_name=_TABLE)
    dialect = op.get_context().dialect.name
    if dialect != "sqlite":
        op.drop_constraint(_CK_SHA, _TABLE, type_="check")
    op.drop_table(_TABLE)
