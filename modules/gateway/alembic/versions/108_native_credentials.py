"""Metadata-only native private credential enrollment and one-shot issuance."""

import sqlalchemy as sa

from alembic import op

revision = "108_native_credentials"
down_revision = "107_repository_eval_display"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "native_credential_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("team_id", sa.String(255), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("execution_id", sa.String(36), nullable=False, unique=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("credential_digest", sa.String(64), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
        sa.Column("offer", sa.JSON()),
        sa.Column("snapshot", sa.String(64)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("browser_session_id", sa.String(64)),
        sa.Column("approval_nonce_hash", sa.String(64)),
        sa.Column("approved_snapshot", sa.String(64)),
        sa.Column("challenge", sa.String(64)),
        sa.Column("challenge_expires_at", sa.Integer()),
        sa.Column("issued_at", sa.Integer()),
        sa.Column("credential_expires_at", sa.Integer()),
        sa.Column("envelope_digest", sa.String(64)),
        sa.Column("acknowledgment", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "user_id", "request_id", name="uq_native_credential_request"),
    )
    op.create_index("ix_native_credential_requests_org_id", "native_credential_requests", ["org_id"])


def downgrade():
    op.drop_table("native_credential_requests")
