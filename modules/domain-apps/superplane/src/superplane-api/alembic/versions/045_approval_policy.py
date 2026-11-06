"""Record the deployment-selected approval policy without rewriting old decisions."""

import sqlalchemy as sa
from alembic import op

revision = "045_approval_policy"
down_revision = "044_organization_grant_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operation_approvals",
        sa.Column(
            "requires_distinct_approver",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM operation_approvals WHERE NOT requires_distinct_approver)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot remove recorded streamlined approval policy")
    op.drop_column("operation_approvals", "requires_distinct_approver")
