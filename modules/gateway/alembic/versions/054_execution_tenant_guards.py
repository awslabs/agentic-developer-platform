"""Make execution-ledger graph bindings tenant-safe.

Correction for #5142.  Revision 052 used globally-valid single-column foreign
keys.  Those prove that an id exists, but not that it belongs to the row's
``org_id``.  The composite constraints below make tenant membership a database
invariant for execution -> flow, execution -> node, and action -> execution.
"""

from alembic import op

revision = "054_execution_tenant_guards"
down_revision = "053_flow_slug_unique"
branch_labels = None
depends_on = None

FLOW_IDENTITY = "uq_orchestration_flows_org_id_id"
NODE_IDENTITY = "uq_orchestration_nodes_org_id_id"
EXECUTION_IDENTITY = "uq_orchestration_executions_org_id_id"
EXECUTION_FLOW_FK = "fk_orchestration_executions_org_flow"
EXECUTION_NODE_FK = "fk_orchestration_executions_org_node"
ACTION_EXECUTION_FK = "fk_orchestration_actions_org_execution"


def _create_tenant_fk(table: str, name: str, local: list[str], remote_table: str, remote: list[str]) -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(table, recreate="always") as batch_op:
            batch_op.create_foreign_key(name, remote_table, local, remote, ondelete="CASCADE")
    else:
        op.create_foreign_key(name, table, remote_table, local, remote, ondelete="CASCADE")


def _drop_tenant_fk(table: str, name: str) -> None:
    if op.get_context().dialect.name == "sqlite":
        with op.batch_alter_table(table, recreate="always") as batch_op:
            batch_op.drop_constraint(name, type_="foreignkey")
    else:
        op.drop_constraint(name, table, type_="foreignkey")


def upgrade() -> None:
    # PostgreSQL requires the referenced composite columns to be unique even
    # though ``id`` alone is already a primary key.  These indexes also make the
    # tenant-leading relationship explicit to schema inspection.
    op.create_index(FLOW_IDENTITY, "orchestration_flows", ["org_id", "id"], unique=True)
    op.create_index(NODE_IDENTITY, "orchestration_nodes", ["org_id", "id"], unique=True)
    op.create_index(EXECUTION_IDENTITY, "orchestration_executions", ["org_id", "id"], unique=True)

    _create_tenant_fk(
        "orchestration_executions",
        EXECUTION_FLOW_FK,
        ["org_id", "flow_id"],
        "orchestration_flows",
        ["org_id", "id"],
    )
    _create_tenant_fk(
        "orchestration_executions",
        EXECUTION_NODE_FK,
        ["org_id", "node_id"],
        "orchestration_nodes",
        ["org_id", "id"],
    )
    _create_tenant_fk(
        "orchestration_actions",
        ACTION_EXECUTION_FK,
        ["org_id", "execution_id"],
        "orchestration_executions",
        ["org_id", "id"],
    )


def downgrade() -> None:
    _drop_tenant_fk("orchestration_actions", ACTION_EXECUTION_FK)
    _drop_tenant_fk("orchestration_executions", EXECUTION_NODE_FK)
    _drop_tenant_fk("orchestration_executions", EXECUTION_FLOW_FK)

    op.drop_index(EXECUTION_IDENTITY, table_name="orchestration_executions")
    op.drop_index(NODE_IDENTITY, table_name="orchestration_nodes")
    op.drop_index(FLOW_IDENTITY, table_name="orchestration_flows")
