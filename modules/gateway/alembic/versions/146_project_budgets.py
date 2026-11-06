"""Base Projects and immutable execution/cost attribution."""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa

from alembic import op

revision = "146_project_budgets"
down_revision = "145_conversation_list_indexes"
branch_labels = None
depends_on = None


def add_column(table, column):
    if column.name not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}:
        op.add_column(table, column)


def create_index(name, table, columns, **kwargs):
    if name not in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns, **kwargs)


def upgrade():
    add_column("projects", sa.Column("budget_meter_initialized", sa.Boolean(), nullable=False, server_default=sa.false()))
    add_column("product_project_bindings", sa.Column("is_base", sa.Boolean(), nullable=False, server_default=sa.false()))
    create_index(
        "uq_product_base_project",
        "product_project_bindings",
        ["tenant_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL AND is_base"),
        sqlite_where=sa.text("valid_to IS NULL AND is_base = 1"),
    )
    db = op.get_bind()
    metadata = sa.MetaData()
    products = sa.Table("products", metadata, autoload_with=db)
    projects = sa.Table("projects", metadata, autoload_with=db)
    bindings = sa.Table("product_project_bindings", metadata, autoload_with=db)
    now = datetime.now(UTC)
    # Preserve the original initial Project and its name/configuration. Products
    # with no active Project receive a deterministic new Base Project. Never
    # reassign an existing Project or manufacture historical spend attribution.
    for product in db.execute(sa.select(products)).mappings():
        current = db.execute(
            sa.select(bindings.c.id)
            .join(
                projects,
                sa.and_(
                    projects.c.tenant_id == bindings.c.tenant_id,
                    projects.c.id == bindings.c.project_id,
                ),
            )
            .where(
                bindings.c.tenant_id == product["tenant_id"],
                bindings.c.product_id == product["id"],
                bindings.c.valid_to.is_(None),
                projects.c.archived_at.is_(None),
            )
            .order_by(bindings.c.is_base.desc(), bindings.c.valid_from, bindings.c.id)
            .limit(1)
        ).scalar_one_or_none()
        if current:
            db.execute(bindings.update().where(bindings.c.tenant_id == product["tenant_id"], bindings.c.id == current).values(is_base=True))
            continue
        project_id = str(uuid5(NAMESPACE_URL, f"adp:base-project:{product['tenant_id']}:{product['id']}"))
        db.execute(
            projects.insert().values(
                tenant_id=product["tenant_id"],
                id=project_id,
                name="Base",
                owner_user_id=product["owner_user_id"],
                state="registered",
                configuration={},
                provenance=[],
                created_at=now,
                updated_at=now,
            )
        )
        db.execute(
            bindings.insert().values(
                tenant_id=product["tenant_id"],
                id=str(uuid5(NAMESPACE_URL, f"adp:base-binding:{project_id}")),
                product_id=product["id"],
                project_id=project_id,
                is_base=True,
                valid_from=now,
                recorded_at=now,
                relationship_revision=1,
                mapping_version="profile-registry-1",
                source_connection_id="adp.profile",
                provenance={"source": "base-project-migration"},
            )
        )
    add_column("orchestration_flows", sa.Column("project_id", sa.String(255)))
    create_index("ix_orchestration_flows_project_id", "orchestration_flows", ["project_id"])
    db.execute(
        sa.text("""
        UPDATE orchestration_flows SET project_id = (
            SELECT b.project_id FROM product_project_bindings b
            WHERE b.tenant_id = orchestration_flows.org_id AND b.product_id = orchestration_flows.product_id
            AND b.is_base = true AND b.valid_to IS NULL
        ) WHERE product_id IS NOT NULL AND project_id IS NULL
    """)
    )
    if not sa.inspect(db).has_table("project_runs"):
        op.create_table(
            "project_runs",
            sa.Column("tenant_id", sa.String(255), primary_key=True),
            sa.Column("run_id", sa.String(255), primary_key=True),
            sa.Column("product_id", sa.String(36), nullable=False),
            sa.Column("project_id", sa.String(255), nullable=False),
            sa.Column("flow_id", sa.String(36)),
            sa.Column("parent_run_id", sa.String(255)),
            sa.Column("owner_user_id", sa.String(255), nullable=False),
            sa.Column("relationship_revision", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
            sa.ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        )
    create_index("ix_project_runs_project", "project_runs", ["tenant_id", "project_id", "created_at"])
    create_index("ix_project_runs_flow", "project_runs", ["tenant_id", "flow_id"])
    for name, length in (("product_id", 36), ("project_id", 255), ("flow_id", 36)):
        add_column("usage_logs", sa.Column(name, sa.String(length)))
        create_index(f"ix_usage_logs_{name}", "usage_logs", [name])


def downgrade():
    # Attribution is financial history. Retain it on code rollback.
    pass
