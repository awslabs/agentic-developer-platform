"""Append fresh human decisions without changing consumed approvals."""

import sqlalchemy as sa
from alembic import op

revision = "046_dispatch_reauthorization"
down_revision = "045_approval_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operation_reauthorizations",
        sa.Column("reauthorization_id", sa.String(64), primary_key=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("original_approval_id", sa.String(64), nullable=False),
        *[
            sa.Column(name, sa.String(255), nullable=False)
            for name in (
                "operation_id",
                "org_id",
                "workspace_id",
                "requester",
                "approved_by",
            )
        ],
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("expected_version", sa.BigInteger(), nullable=False),
        sa.Column("result", sa.String(32)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("org_id", "idempotency_key"),
    )
    op.execute("""CREATE FUNCTION operation_reauthorization_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'reauthorization history is immutable'; END IF;
        IF (to_jsonb(NEW)-'result'-'decided_at'-'revoked') IS DISTINCT FROM
           (to_jsonb(OLD)-'result'-'decided_at'-'revoked') OR
           (OLD.result IS NOT NULL AND (NEW.result IS DISTINCT FROM OLD.result OR
                                       NEW.decided_at IS DISTINCT FROM OLD.decided_at)) OR
           (OLD.revoked AND NOT NEW.revoked)
        THEN RAISE EXCEPTION 'reauthorization history is immutable'; END IF;
        RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER operation_reauthorization_immutable
        BEFORE UPDATE OR DELETE ON operation_reauthorizations FOR EACH ROW
        EXECUTE FUNCTION operation_reauthorization_immutable()""")


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS(SELECT 1 FROM operation_reauthorizations)"))
        .scalar()
    ):
        raise RuntimeError("cannot discard reauthorization history")
    op.drop_table("operation_reauthorizations")
    op.execute("DROP FUNCTION operation_reauthorization_immutable()")
