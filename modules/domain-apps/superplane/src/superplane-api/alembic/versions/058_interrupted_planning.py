"""Separate interrupted read-only planning from unstarted planning."""

import sqlalchemy as sa
from alembic import op

revision = "058_interrupted_planning"
down_revision = "057_bootstrap_recovery_terms"
branch_labels = None
depends_on = None


def constraints(interrupted):
    scopes = "'undispatched','uncredentialed-planning','unstarted-planning','planning-budget','saved-plan-completion','partial-apply-repair','unstarted-bootstrap'"
    terms = "'planning-budget','unstarted-planning','saved-plan-completion','partial-apply-repair','unstarted-bootstrap'"
    if interrupted:
        scopes += ",'interrupted-planning'"
        terms += ",'interrupted-planning'"
    for name, expression in (
        ("operation_reauthorization_scope", "scope IN (" + scopes + ")"),
        (
            "planning_budget_terms",
            "(scope NOT IN ('planning-budget','partial-apply-repair') OR amendment_json IS NOT NULL) AND (amendment_json IS NULL OR scope IN ("
            + terms
            + "))",
        ),
    ):
        op.drop_constraint(name, "operation_reauthorizations", type_="check")
        op.create_check_constraint(name, "operation_reauthorizations", expression)


def upgrade():
    constraints(True)


def downgrade():
    op.execute("LOCK TABLE operation_reauthorizations IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_reauthorizations WHERE scope='interrupted-planning')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard interrupted planning decisions")
    constraints(False)
