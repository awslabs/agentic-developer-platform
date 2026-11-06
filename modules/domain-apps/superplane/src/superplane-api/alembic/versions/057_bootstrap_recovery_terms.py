"""Permit the explicitly reviewed runtime terms of an original bootstrap retry."""

import sqlalchemy as sa
from alembic import op

revision = "057_bootstrap_recovery_terms"
down_revision = "056_recovery_settlement_claims"
branch_labels = None
depends_on = None


def constraint(bootstrap):
    scopes = "'planning-budget','unstarted-planning','saved-plan-completion','partial-apply-repair'"
    if bootstrap:
        scopes += ",'unstarted-bootstrap'"
    op.drop_constraint(
        "planning_budget_terms", "operation_reauthorizations", type_="check"
    )
    op.create_check_constraint(
        "planning_budget_terms",
        "operation_reauthorizations",
        "(scope NOT IN ('planning-budget','partial-apply-repair') OR amendment_json IS NOT NULL) AND "
        "(amendment_json IS NULL OR scope IN (" + scopes + "))",
    )


def upgrade():
    constraint(True)


def downgrade():
    op.execute("LOCK TABLE operation_reauthorizations IN ACCESS EXCLUSIVE MODE")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_reauthorizations "
                "WHERE scope='unstarted-bootstrap' AND amendment_json IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard bootstrap runtime review history")
    constraint(False)
