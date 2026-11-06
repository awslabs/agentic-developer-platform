"""Scope the owner-proven initial planning restart separately from credential refusal."""

import sqlalchemy as sa
from alembic import op

revision = "048_planning_recovery"
down_revision = "047_provider_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "operation_reauthorization_scope", "operation_reauthorizations", type_="check"
    )
    op.create_check_constraint(
        "operation_reauthorization_scope",
        "operation_reauthorizations",
        "scope IN ('undispatched','uncredentialed-planning','unstarted-planning')",
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_reauthorizations WHERE scope='unstarted-planning')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard planning recovery decision history")
    op.drop_constraint(
        "operation_reauthorization_scope", "operation_reauthorizations", type_="check"
    )
    op.create_check_constraint(
        "operation_reauthorization_scope",
        "operation_reauthorizations",
        "scope IN ('undispatched','uncredentialed-planning')",
    )
