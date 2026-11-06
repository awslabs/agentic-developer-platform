"""Retain explicit unchanged-budget confirmation in a fresh planning recovery."""

import sqlalchemy as sa
from alembic import op

revision = "050_planning_budget_confirmation"
down_revision = "049_planning_budget"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "planning_budget_terms", "operation_reauthorizations", type_="check"
    )
    op.create_check_constraint(
        "planning_budget_terms",
        "operation_reauthorizations",
        "(scope != 'planning-budget' OR amendment_json IS NOT NULL) AND "
        "(amendment_json IS NULL OR scope IN ('planning-budget','unstarted-planning'))",
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_reauthorizations "
                "WHERE scope='unstarted-planning' AND amendment_json IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard reviewed budget confirmation history")
    op.drop_constraint(
        "planning_budget_terms", "operation_reauthorizations", type_="check"
    )
    op.create_check_constraint(
        "planning_budget_terms",
        "operation_reauthorizations",
        "(scope='planning-budget') = (amendment_json IS NOT NULL)",
    )
