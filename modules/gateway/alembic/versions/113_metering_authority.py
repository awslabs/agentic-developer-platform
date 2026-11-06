"""Add durable scoped metering authority without removing legacy budget state.

Revision ID: 113_metering_authority
Revises: 112_product_profile_foundation
"""

import sqlalchemy as sa

from alembic import op

revision = "113_metering_authority"
down_revision = "112_product_profile_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "metering_scope_gates",
        sa.Column("scope_key", sa.String(512), primary_key=True),
        sa.Column("scope_kind", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(255)),
        sa.Column("activation", sa.String(16), nullable=False, server_default="legacy"),
        sa.Column("readiness", sa.String(32), nullable=False, server_default="unverified"),
        sa.Column("policy_revision", sa.String(128)),
        sa.Column("binding_revision", sa.String(128)),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("activation IN ('legacy','draining','active')", name="ck_metering_gate_activation"),
        sa.CheckConstraint("revision >= 1", name="ck_metering_gate_revision"),
        sa.CheckConstraint(
            "(scope_kind = 'person' AND tenant_id IS NULL) OR (scope_kind <> 'person' AND tenant_id IS NOT NULL)", name="ck_metering_gate_tenant"
        ),
    )
    op.create_index("ix_metering_gate_tenant", "metering_scope_gates", ["tenant_id", "scope_kind"])
    op.create_table(
        "metering_attempts",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("request_id", sa.String(255), primary_key=True),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("submitted_sha256", sa.String(64), nullable=False),
        sa.Column("pricing_revision", sa.String(128), nullable=False),
        sa.Column("quote_snapshot", sa.JSON(), nullable=False),
        sa.Column("attribution_snapshot", sa.JSON(), nullable=False),
        sa.Column("bound_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("producer_epoch", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("prepare_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatch_deadline", sa.DateTime(timezone=True)),
        sa.Column("completion_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("admission_key", sa.String(768), nullable=False),
        sa.Column("admission_version", sa.String(255)),
        sa.Column("admission_sha256", sa.String(64)),
        sa.Column("final_key", sa.String(768), nullable=False),
        sa.Column("final_version", sa.String(255)),
        sa.Column("final_sha256", sa.String(64)),
        sa.Column("reconciliation_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("bound_usd >= 0", name="ck_metering_attempt_bound"),
        sa.CheckConstraint("revision >= 1 AND producer_epoch >= 1", name="ck_metering_attempt_revision"),
        sa.CheckConstraint(
            "state IN ('prepared','dispatch_armed','unknown','quarantined','settled','cancelled_unsubmitted')",
            name="ck_metering_attempt_state",
        ),
        sa.CheckConstraint(
            "(admission_version IS NULL AND admission_sha256 IS NULL) OR (admission_version IS NOT NULL AND admission_sha256 IS NOT NULL)",
            name="ck_metering_attempt_admission_receipt",
        ),
        sa.CheckConstraint(
            "(final_version IS NULL AND final_sha256 IS NULL) OR (final_version IS NOT NULL AND final_sha256 IS NOT NULL)",
            name="ck_metering_attempt_final_receipt",
        ),
        sa.CheckConstraint(
            "state <> 'dispatch_armed' OR (admission_version IS NOT NULL AND dispatch_deadline IS NOT NULL)",
            name="ck_metering_attempt_dispatch",
        ),
        sa.CheckConstraint(
            "state <> 'settled' OR final_version IS NOT NULL",
            name="ck_metering_attempt_settled_receipt",
        ),
    )
    op.create_index("ix_metering_attempt_due", "metering_attempts", ["state", "completion_deadline", "tenant_id", "request_id"])
    op.create_table(
        "metering_attempt_scopes",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("request_id", sa.String(255), primary_key=True),
        sa.Column("scope_key", sa.String(512), sa.ForeignKey("metering_scope_gates.scope_key", ondelete="RESTRICT"), primary_key=True),
        sa.Column("allocation_period", sa.String(64), primary_key=True),
        sa.Column("bound_usd", sa.Numeric(20, 6), nullable=False),
        sa.Column("captured_allocation", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "request_id"], ["metering_attempts.tenant_id", "metering_attempts.request_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("bound_usd >= 0", name="ck_metering_scope_bound"),
    )
    op.create_index("ix_metering_scope_exposure", "metering_attempt_scopes", ["scope_key", "allocation_period", "tenant_id", "request_id"])
    op.create_table(
        "metering_scope_aliases",
        sa.Column("alias_kind", sa.String(32), primary_key=True),
        sa.Column("stable_alias", sa.String(255), primary_key=True),
        sa.Column("scope_key", sa.String(512), sa.ForeignKey("metering_scope_gates.scope_key", ondelete="RESTRICT"), primary_key=True),
        sa.Column("identity_revision", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_metering_alias_lookup", "metering_scope_aliases", ["alias_kind", "stable_alias"])
    op.create_table(
        "metering_reconciliation_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("request_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("authority", sa.String(128), nullable=False),
        sa.Column("prior_revision", sa.BigInteger(), nullable=False),
        sa.Column("new_revision", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(1024), nullable=False),
        sa.Column("evidence_ref", sa.String(768), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("receipt_sha256", sa.String(64)),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "request_id"], ["metering_attempts.tenant_id", "metering_attempts.request_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "request_id", "idempotency_key", name="uq_metering_event_idempotency"),
        sa.CheckConstraint("new_revision >= prior_revision", name="ck_metering_event_revision"),
    )
    op.create_index("ix_metering_events_attempt", "metering_reconciliation_events", ["tenant_id", "request_id", "created_at"])


def downgrade():
    raise RuntimeError("Metering evidence and unresolved holds must survive rollback")
