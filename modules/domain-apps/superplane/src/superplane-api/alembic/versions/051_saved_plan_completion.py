"""Separate saved-plan finalization authority from unstarted-plan recovery."""

import sqlalchemy as sa
from alembic import op

revision = "051_saved_plan_completion"
down_revision = "050_planning_budget_confirmation"
branch_labels = None
depends_on = None


def constraints(scopes, budget_scopes):
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
        "(scope != 'planning-budget' OR amendment_json IS NOT NULL) AND "
        "(amendment_json IS NULL OR scope IN (" + budget_scopes + "))",
    )


def upgrade():
    constraints(
        "'undispatched','uncredentialed-planning','unstarted-planning','planning-budget','saved-plan-completion'",
        "'planning-budget','unstarted-planning','saved-plan-completion'",
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_reauthorizations WHERE scope='saved-plan-completion')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard saved plan completion authority")
    constraints(
        "'undispatched','uncredentialed-planning','unstarted-planning','planning-budget'",
        "'planning-budget','unstarted-planning'",
    )
