"""Separate original bootstrap retry from planning and partial apply recovery."""

import sqlalchemy as sa
from alembic import op

revision = "054_unstarted_bootstrap"
down_revision = "053_partial_preparation_refresh"
branch_labels = None
depends_on = None


def constraint(bootstrap):
    scopes = "'undispatched','uncredentialed-planning','unstarted-planning','planning-budget','saved-plan-completion','partial-apply-repair'"
    if bootstrap:
        scopes += ",'unstarted-bootstrap'"
    op.drop_constraint(
        "operation_reauthorization_scope", "operation_reauthorizations", type_="check"
    )
    op.create_check_constraint(
        "operation_reauthorization_scope",
        "operation_reauthorizations",
        "scope IN (" + scopes + ")",
    )


def upgrade():
    constraint(True)


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_reauthorizations WHERE scope='unstarted-bootstrap')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard bootstrap retry history")
    constraint(False)
