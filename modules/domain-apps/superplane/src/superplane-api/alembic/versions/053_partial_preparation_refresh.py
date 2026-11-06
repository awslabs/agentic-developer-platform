"""Preserve prior preparations while admitting a freshly inspected request."""

from alembic import op
import sqlalchemy as sa

revision = "053_partial_preparation_refresh"
down_revision = "052_partial_apply_repair"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        "uq_partial_apply_preparation",
        "workspace_partial_apply_preparations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_partial_apply_preparation",
        "workspace_partial_apply_preparations",
        [
            "org_id",
            "workspace_id",
            "source_operation_id",
            "source_fence",
            "owner_evidence_sha256",
        ],
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM workspace_partial_apply_preparations "
                "GROUP BY org_id,workspace_id,source_operation_id,source_fence HAVING count(*)>1)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("cannot discard fresh preparation history")
    op.drop_constraint(
        "uq_partial_apply_preparation",
        "workspace_partial_apply_preparations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_partial_apply_preparation",
        "workspace_partial_apply_preparations",
        ["org_id", "workspace_id", "source_operation_id", "source_fence"],
    )
