"""Bind a fresh planning-recovery decision separately from undispatched recovery."""

import sqlalchemy as sa
from alembic import op

revision = "047_provider_recovery"
down_revision = "046_dispatch_reauthorization"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "operation_reauthorizations",
        sa.Column(
            "scope", sa.String(32), nullable=False, server_default="undispatched"
        ),
    )
    op.create_check_constraint(
        "operation_reauthorization_scope",
        "operation_reauthorizations",
        "scope IN ('undispatched','uncredentialed-planning')",
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_reauthorizations WHERE scope<>'undispatched')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard provider recovery decision history")
    op.drop_constraint(
        "operation_reauthorization_scope", "operation_reauthorizations", type_="check"
    )
    op.drop_column("operation_reauthorizations", "scope")
