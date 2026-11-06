"""Retain tenant-local Security human/effect and release exchange evidence."""

import sqlalchemy as sa

from alembic import op

revision = "178_security_manual_evidence"
down_revision = "177_l5_wa_periodic_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_security_evidence_events",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_l5_security_evidence_request", "l5_security_evidence_events", ["tenant_id", "request_id", "kind"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION l5_security_evidence_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'Security evidence is append-only'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER l5_security_evidence_immutable BEFORE UPDATE OR DELETE ON l5_security_evidence_events "
            "FOR EACH ROW EXECUTE FUNCTION l5_security_evidence_immutable()"
        )

    elif op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER l5_security_evidence_{operation.lower()} "
                f"BEFORE {operation} ON l5_security_evidence_events "
                "BEGIN SELECT RAISE(ABORT, 'Security evidence is append-only'); END"
            )


def downgrade():
    raise RuntimeError("The L5 release retains owner authorization and qualification history and cannot safely be reversed")
