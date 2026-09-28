"""Durable admission for faithful model-invocability probes — PMM-03."""

import sqlalchemy as sa

from alembic import op

revision = "058_model_probe_admission"
down_revision = "057_model_invocability_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_probe_cycles",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("cycle_key", sa.String(255), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("max_slots", sa.Integer(), nullable=False),
        sa.Column("budget_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("started_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("trigger IN ('scheduled', 'manual', 'change')", name="ck_model_probe_cycle_trigger"),
        sa.CheckConstraint("status IN ('active', 'completed', 'expired')", name="ck_model_probe_cycle_status"),
        sa.CheckConstraint("max_slots >= 0", name="ck_model_probe_cycle_slots"),
        sa.CheckConstraint(
            "budget_usd >= 0 AND reserved_usd >= 0 AND started_usd >= 0 AND reserved_usd <= budget_usd AND started_usd <= reserved_usd",
            name="ck_model_probe_cycle_spend",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cycle_key", name="uq_model_probe_cycle_key"),
    )
    op.create_index(
        "ix_model_probe_cycle_status_expiry",
        "model_probe_cycles",
        ["status", "expires_at"],
    )

    op.create_table(
        "model_probe_slots",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("cycle_id", sa.String(36), nullable=False),
        sa.Column("destination_id", sa.String(255), nullable=False),
        sa.Column("account_id", sa.String(12), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("canonical_model_id", sa.String(255), nullable=False),
        sa.Column("compatibility_class", sa.String(64), nullable=False),
        sa.Column("harness_contract_revision", sa.String(32), nullable=False),
        sa.Column("expected_request_shape_sha256", sa.String(64), nullable=False),
        sa.Column("reserved_budget_usd", sa.Numeric(18, 6), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token_sha256", sa.String(64), nullable=False),
        sa.Column("paid_attempt_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("observed_request_shape_sha256", sa.String(64), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("completion_fingerprint", sa.String(64), nullable=True),
        sa.Column("indeterminate_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved', 'started', 'completed', 'indeterminate', 'expired')",
            name="ck_model_probe_slot_status",
        ),
        sa.CheckConstraint("reserved_budget_usd > 0", name="ck_model_probe_slot_budget"),
        sa.CheckConstraint(
            "(status = 'reserved' AND paid_attempt_started_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'started' AND paid_attempt_started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND paid_attempt_started_at IS NOT NULL AND completed_at IS NOT NULL "
            " AND outcome IS NOT NULL AND completion_fingerprint IS NOT NULL) OR "
            "(status = 'indeterminate' AND paid_attempt_started_at IS NOT NULL) OR "
            "(status = 'expired' AND paid_attempt_started_at IS NULL)",
            name="ck_model_probe_slot_state",
        ),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('proven', 'refused', 'error')", name="ck_model_probe_slot_outcome"),
        sa.ForeignKeyConstraint(["cycle_id"], ["model_probe_cycles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cycle_id",
            "destination_id",
            "canonical_model_id",
            "compatibility_class",
            "harness_contract_revision",
            "expected_request_shape_sha256",
            name="uq_model_probe_slot_candidate",
        ),
    )
    op.create_index("ix_model_probe_slot_cycle_status", "model_probe_slots", ["cycle_id", "status"])
    op.create_index("ix_model_probe_slot_lease_expiry", "model_probe_slots", ["lease_expires_at"])

    if op.get_context().dialect.name != "sqlite":
        op.create_check_constraint(
            "ck_model_probe_slot_expected_sha256",
            "model_probe_slots",
            sa.text("expected_request_shape_sha256 ~ '^[0-9a-f]{64}$'"),
        )
        op.create_check_constraint(
            "ck_model_probe_slot_observed_sha256",
            "model_probe_slots",
            sa.text("observed_request_shape_sha256 IS NULL OR observed_request_shape_sha256 ~ '^[0-9a-f]{64}$'"),
        )
        op.create_check_constraint(
            "ck_model_probe_slot_lease_token_sha256",
            "model_probe_slots",
            sa.text("lease_token_sha256 ~ '^[0-9a-f]{64}$'"),
        )


def downgrade() -> None:
    op.drop_table("model_probe_slots")
    op.drop_table("model_probe_cycles")
