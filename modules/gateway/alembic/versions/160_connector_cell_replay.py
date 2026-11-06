"""Scope operation replay uniqueness to the admitted cell."""

import sqlalchemy as sa

from alembic import op

revision = "160_connector_cell_replay"
down_revision = "159_connector_module_admission"
branch_labels = None
depends_on = None


def upgrade():
    old = next(
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints("connector_operation_contexts")
        if constraint["column_names"] == ["tenant_id", "consumer", "operation_id", "attempt_id"]
    )
    op.drop_constraint(old, "connector_operation_contexts", type_="unique")
    op.create_unique_constraint(
        "uq_connector_context_cell_attempt",
        "connector_operation_contexts",
        ["tenant_id", "consumer", "cell_id", "operation_id", "attempt_id"],
    )


def downgrade():
    op.drop_constraint("uq_connector_context_cell_attempt", "connector_operation_contexts", type_="unique")
    op.create_unique_constraint(
        "uq_connector_context_attempt",
        "connector_operation_contexts",
        ["tenant_id", "consumer", "operation_id", "attempt_id"],
    )
