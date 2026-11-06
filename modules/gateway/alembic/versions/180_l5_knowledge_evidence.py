"""Retain attributed knowledge-use evidence without granting native authority."""

import sqlalchemy as sa

from alembic import op

revision = "180_l5_knowledge_evidence"
down_revision = "179_l5_knowledge_effects"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_devops_knowledge_evidence",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("evidence_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_l5_knowledge_evidence_operation", "l5_devops_knowledge_evidence", ["tenant_id", "operation_id", "kind"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION l5_knowledge_evidence_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'Knowledge evidence is append-only'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER l5_knowledge_evidence_immutable BEFORE UPDATE OR DELETE ON l5_devops_knowledge_evidence "
            "FOR EACH ROW EXECUTE FUNCTION l5_knowledge_evidence_immutable()"
        )
    elif op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER l5_knowledge_evidence_{action.lower()} BEFORE {action} ON l5_devops_knowledge_evidence "
                "BEGIN SELECT RAISE(ABORT, 'Knowledge evidence is append-only'); END"
            )


def downgrade():
    raise RuntimeError("The L5 release retains owner authorization and qualification history and cannot safely be reversed")
