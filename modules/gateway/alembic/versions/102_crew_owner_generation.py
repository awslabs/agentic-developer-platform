"""Share a durable owner generation across Crew pods and Dev Boxes."""

import sqlalchemy as sa

from alembic import op

revision = "102_crew_owner_generation"
down_revision = "101_crew_host_fingerprint"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crew_owner_generations",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(255), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.CheckConstraint("generation >= 0", name="ck_crew_owner_generation"),
    )
    connection = op.get_bind()
    for table, tenant, generation in (
        ("crew_executors", "tenant_id", "generation"),
        ("dev_boxes", "org_id", "machine_generation"),
    ):
        rows = connection.execute(sa.text(
            f"SELECT {tenant} AS tenant_id, user_id, MAX({generation}) AS generation "
            f"FROM {table} GROUP BY {tenant}, user_id"
        ))
        for row in rows:
            connection.execute(sa.text("""
                INSERT INTO crew_owner_generations (tenant_id, user_id, generation)
                VALUES (:tenant_id, :user_id, :generation)
                ON CONFLICT (tenant_id, user_id) DO UPDATE SET generation =
                    CASE WHEN excluded.generation > crew_owner_generations.generation
                         THEN excluded.generation ELSE crew_owner_generations.generation END
            """), dict(row._mapping))


def downgrade():
    op.drop_table("crew_owner_generations")
