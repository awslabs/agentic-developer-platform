"""Register ancillary repair planning and separately approved exact-plan completion."""

import sqlalchemy as sa
from alembic import op

revision = "052_partial_apply_repair"
down_revision = "051_saved_plan_completion"
branch_labels = None
depends_on = None


def constraints(partial):
    scopes = "'undispatched','uncredentialed-planning','unstarted-planning','planning-budget','saved-plan-completion'"
    terms = "'planning-budget','unstarted-planning','saved-plan-completion'"
    if partial:
        scopes += ",'partial-apply-repair'"
        terms += ",'partial-apply-repair'"
    for name in ("operation_reauthorization_scope", "planning_budget_terms"):
        op.drop_constraint(name, "operation_reauthorizations", type_="check")
    op.create_check_constraint(
        "operation_reauthorization_scope",
        "operation_reauthorizations",
        "scope IN (" + scopes + ")",
    )
    op.create_check_constraint(
        "planning_budget_terms",
        "operation_reauthorizations",
        "(scope NOT IN ('planning-budget','partial-apply-repair') OR amendment_json IS NOT NULL) AND "
        "(amendment_json IS NULL OR scope IN (" + terms + "))",
    )


def upgrade():
    op.create_table(
        "workspace_partial_apply_preparations",
        sa.Column("operation_id", sa.String(255), primary_key=True),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("workspace_id", sa.String(255), nullable=False),
        sa.Column("source_operation_id", sa.String(255), nullable=False),
        sa.Column("source_fence", sa.BigInteger(), nullable=False),
        sa.Column("source_version", sa.BigInteger(), nullable=False),
        sa.Column("plan_digest", sa.String(64), nullable=False),
        sa.Column("original_allocation_id", sa.String(255), nullable=False),
        sa.Column("owner_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("owner_evidence_json", sa.Text(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "org_id",
            "workspace_id",
            "source_operation_id",
            "source_fence",
            name="uq_partial_apply_preparation",
        ),
        sa.CheckConstraint("source_fence BETWEEN 1 AND 32 AND source_version > 0"),
        sa.CheckConstraint(
            "plan_digest ~ '^[a-f0-9]{64}$' AND owner_evidence_sha256 ~ '^[a-f0-9]{64}$'"
        ),
        sa.CheckConstraint("operation_id <> source_operation_id"),
    )
    op.execute("""CREATE FUNCTION immutable_partial_apply_preparation() RETURNS trigger
    LANGUAGE plpgsql AS $$ BEGIN
      RAISE EXCEPTION 'partial apply preparation history is immutable';
    END $$""")
    op.execute("""CREATE TRIGGER immutable_partial_apply_preparation
    BEFORE UPDATE OR DELETE ON workspace_partial_apply_preparations
    FOR EACH ROW EXECUTE FUNCTION immutable_partial_apply_preparation()""")
    constraints(True)


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM workspace_partial_apply_preparations) OR "
                "EXISTS(SELECT 1 FROM operation_reauthorizations WHERE scope='partial-apply-repair')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard partial apply recovery history")
    constraints(False)
    op.drop_table("workspace_partial_apply_preparations")
    op.execute("DROP FUNCTION immutable_partial_apply_preparation()")
