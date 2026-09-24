"""Keep batch and serving identities separate within the shared quota owner."""

from alembic import op
import sqlalchemy as sa

revision = "032_batch_workload_kind"
down_revision = "031_controller_deployment_registry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "deployments",
        sa.Column(
            "workload_kind", sa.String(16), nullable=False, server_default="serving"
        ),
    )
    op.create_check_constraint(
        "ck_deployments_workload_kind",
        "deployments",
        "workload_kind IN ('serving','batch')",
    )


def downgrade():
    # Older code interprets every row as serving. Never relabel a retained Job.
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM deployments WHERE workload_kind='batch') THEN
            RAISE EXCEPTION 'Retained batch records require this schema; preserve them before rollback';
        END IF;
    END $$""")
    op.drop_constraint("ck_deployments_workload_kind", "deployments", type_="check")
    op.drop_column("deployments", "workload_kind")
