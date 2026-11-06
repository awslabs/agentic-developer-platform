"""Append reviewed daily-budget amendments without rewriting admitted requests."""

import sqlalchemy as sa
from alembic import op

revision = "049_planning_budget"
down_revision = "048_planning_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "operation_reauthorizations",
        sa.Column("amendment_json", sa.Text(), nullable=True),
    )
    op.drop_constraint(
        "operation_reauthorization_scope", "operation_reauthorizations", type_="check"
    )
    op.create_check_constraint(
        "operation_reauthorization_scope",
        "operation_reauthorizations",
        "scope IN ('undispatched','uncredentialed-planning','unstarted-planning','planning-budget')",
    )
    op.create_check_constraint(
        "planning_budget_terms",
        "operation_reauthorizations",
        "(scope='planning-budget') = (amendment_json IS NOT NULL)",
    )
    op.create_index(
        "one_approved_planning_budget",
        "operation_reauthorizations",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("scope='planning-budget' AND result='allowed-once'"),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_reauthorizations WHERE scope='planning-budget')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard reviewed budget history")
    op.drop_index(
        "one_approved_planning_budget", table_name="operation_reauthorizations"
    )
    op.drop_constraint(
        "planning_budget_terms", "operation_reauthorizations", type_="check"
    )
    op.drop_constraint(
        "operation_reauthorization_scope", "operation_reauthorizations", type_="check"
    )
    op.create_check_constraint(
        "operation_reauthorization_scope",
        "operation_reauthorizations",
        "scope IN ('undispatched','uncredentialed-planning','unstarted-planning')",
    )
    op.drop_column("operation_reauthorizations", "amendment_json")
